"""Host-side half of the rollout executor: switch ONE worker unit to a release directory, or put it back.

Runs ON the worker's host (the reconciler pipes this file's source over ssh: `python3 - <op> '<json>'`), so it
imports nothing from livestack_node and needs only Python >= 3.6. It prints one JSON object and exits 0 when the
operation succeeded, 1 when it failed with a named `failure`.

    install   {"release": NAME, "content_hash": H, "releases_dir": D}   (after a push into .stage-NAME/NAME)
    status    {"worker": W, "release": NAME?}            unit state, running release and its content hash;
                                                          with `release`, whether that directory is installed
    switch    {"worker": W, "release": NAME, "content_hash": H, "releases_dir": D}
    rollback  {"worker": W, "release": NAME}   (only undoes the switch to NAME made within the last 2 h)
    smoke     {"worker": W, "release": NAME, "releases_dir": D, "probes": [...], "window_s": N, "modules": [...]?}

How a release is selected, and why this is safe to undo: systemd applies drop-ins in file-name order and the last
`Environment=PYTHONPATH=` wins. The managed drop-in is named `~rollout-release.conf`; `~` (0x7e) sorts after every
other name, so it always wins and nothing else is renamed or deleted. It replaces ONLY the `<releases_dir>/<name>/node-py`
component of the unit's effective PYTHONPATH (a unit may carry other entries, e.g. vendored dependencies). The previous
state is the file's absence (or its previous bytes, recorded under ~/.local/state/livestack-rollout/<unit>/), so
`rollback` is: restore or remove that one file, daemon-reload, restart.

It never restarts a unit that has an attempt in flight (state_dir/active.json present) and never touches the
authority: the caller drains and waits for idle first, this is the second lock.
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser('~')
UNIT_DIR = os.path.join(HOME, '.config/systemd/user')
STATE_ROOT = os.path.join(HOME, '.local/state/livestack-rollout')
MANAGED = '~rollout-release.conf'
DEFAULT_RELEASES = os.path.join(HOME, '.local/share/livestack-workload-releases')
RELEASE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}$')


class Fail(Exception):
    def __init__(self, failure, **detail):
        Exception.__init__(self, failure)
        self.failure, self.detail = failure, detail


def out(ok, **fields):
    print(json.dumps(dict(ok=ok, **fields), separators=(',', ':')))
    return 0 if ok else 1


def run(argv, timeout=60):
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout.decode('utf-8', 'replace')


def systemctl(*args, timeout=60):
    return run(['systemctl', '--user'] + list(args), timeout)


def content_hash(release_dir):
    """Same algorithm as node-py/scripts/build-worker-release.py content_hash/tree_digests."""
    base = os.path.join(release_dir, 'node-py')
    digests = {}
    for dp, dn, fn in os.walk(base):
        dn[:] = [d for d in dn if d != '__pycache__']
        for f in fn:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, base).replace(os.sep, '/')
            if f == 'RELEASE.json' or f.endswith('.pyc') or os.path.islink(p):
                continue
            h = hashlib.sha256()
            with open(p, 'rb') as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b''):
                    h.update(chunk)
            digests[rel] = h.hexdigest()
    top = hashlib.sha256()
    for rel in sorted(digests):
        top.update(('%s\0%s\n' % (rel, digests[rel])).encode())
    return top.hexdigest()


def find_unit(worker):
    """(unit name, worker config dict) of the worker_service unit whose --config names this worker id."""
    for frag in sorted(glob.glob(os.path.join(UNIT_DIR, 'livestack-workload-worker*.service'))):
        text = open(frag, errors='replace').read()
        m = re.search(r'--config[ =]+(\S+)', text)
        if not m or 'worker_service' not in text:
            continue
        try:
            cfg = json.load(open(m.group(1).replace('%h', HOME)))
        except Exception:
            continue
        if cfg.get('worker') == worker:
            return os.path.basename(frag)[:-len('.service')], cfg
    raise Fail('unit_not_found', worker=worker)


def props(unit):
    rc, text = systemctl('show', unit + '.service', '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID',
                         '-p', 'NRestarts', '-p', 'Environment')
    d = dict(l.split('=', 1) for l in text.splitlines() if '=' in l)
    m = None
    for m in re.finditer(r'(?:^|\s)"?PYTHONPATH=([^"\s]*)', d.get('Environment', '')):
        pass
    d['effective_pythonpath'] = m.group(1) if m else None
    return d


def running_pythonpath(pid):
    try:
        for item in open('/proc/%d/environ' % pid, 'rb').read().split(b'\0'):
            if item.startswith(b'PYTHONPATH='):
                return item[len('PYTHONPATH='):].decode()
    except Exception:
        return None
    return ''


def release_of(pythonpath):
    """(release dir, other PYTHONPATH components) from a PYTHONPATH with exactly one .../node-py entry."""
    parts = (pythonpath or '').split(':')
    rel = [p for p in parts if p.endswith('/node-py')]
    if len(rel) != 1:
        return None, parts
    return os.path.dirname(rel[0]), [p for p in parts if p != rel[0]]


def worker_busy(cfg):
    return os.path.exists(os.path.join(cfg.get('state_dir', ''), 'active.json'))


def wait_healthy(unit, want_pythonpath, seconds=90):
    deadline = time.time() + seconds
    last = {}
    while time.time() < deadline:
        last = props(unit)
        pid = int(last.get('MainPID') or 0)
        if last.get('ActiveState') == 'active' and pid > 1 and running_pythonpath(pid) == want_pythonpath:
            return True, last
        time.sleep(2)
    return False, last


def op_status(a):
    unit, cfg = find_unit(a['worker'])
    p = props(unit)
    pid = int(p.get('MainPID') or 0)
    rel_dir, _ = release_of(running_pythonpath(pid) if pid else p['effective_pythonpath'])
    res = dict(unit=unit, active=p.get('ActiveState'), sub=p.get('SubState'), main_pid=pid, nrestarts=p.get('NRestarts'),
               busy=worker_busy(cfg), managed=os.path.exists(os.path.join(UNIT_DIR, unit + '.service.d', MANAGED)),
               running_pythonpath=running_pythonpath(pid) if pid else None, effective_pythonpath=p['effective_pythonpath'],
               release=os.path.basename(rel_dir) if rel_dir else None,
               content_hash=content_hash(rel_dir) if rel_dir and os.path.isdir(rel_dir) else None)
    if a.get('release'):
        if not RELEASE_NAME.match(a['release']):
            raise Fail('bad_release_name')
        target = os.path.join(a.get('releases_dir') or DEFAULT_RELEASES, a['release'])
        res['target_present'] = os.path.isdir(os.path.join(target, 'node-py', 'livestack_node'))
        res['target_content_hash'] = content_hash(target) if res['target_present'] else None
    return out(True, **res)


def op_install(a):
    """Move a pushed release from `.stage-<name>/<name>` into place if its content hash is the expected one."""
    if not RELEASE_NAME.match(a.get('release', '')):
        raise Fail('bad_release_name')
    releases = a.get('releases_dir') or DEFAULT_RELEASES
    staged = os.path.join(releases, '.stage-' + a['release'], a['release'])
    final = os.path.join(releases, a['release'])
    if not os.path.isdir(staged):
        raise Fail('nothing_staged', release=a['release'])
    got = content_hash(staged)
    if got != a['content_hash']:
        import shutil
        shutil.rmtree(os.path.join(releases, '.stage-' + a['release']), ignore_errors=True)
        raise Fail('staged_content_hash_mismatch', release=a['release'])
    if os.path.exists(final):
        raise Fail('release_already_installed_with_other_content', release=a['release'])
    os.rename(staged, final)
    os.rmdir(os.path.dirname(staged))
    return out(True, release=a['release'], content_hash=got)


def op_switch(a):
    unit, cfg = find_unit(a['worker'])
    if not RELEASE_NAME.match(a.get('release', '')):
        raise Fail('bad_release_name')
    releases = a.get('releases_dir') or DEFAULT_RELEASES
    target = os.path.join(releases, a['release'])
    if not os.path.isdir(os.path.join(target, 'node-py', 'livestack_node')):
        raise Fail('release_missing', release=a['release'])
    if content_hash(target) != a['content_hash']:
        raise Fail('release_content_hash_mismatch', release=a['release'])
    if worker_busy(cfg):
        raise Fail('worker_busy')
    before = props(unit)
    current = before['effective_pythonpath']
    cur_dir, others = release_of(current)
    if cur_dir is None:
        raise Fail('pythonpath_has_no_single_release_entry', pythonpath=current)
    new = ':'.join([os.path.join(target, 'node-py')] + others)
    dropin_dir = os.path.join(UNIT_DIR, unit + '.service.d')
    managed = os.path.join(dropin_dir, MANAGED)
    previous_managed = open(managed).read() if os.path.exists(managed) else None
    state = os.path.join(STATE_ROOT, unit)
    os.makedirs(state, exist_ok=True)
    json.dump(dict(at=time.time(), previous_effective=current, previous_managed=previous_managed, new=new,
                   release=a['release']), open(os.path.join(state, 'last-switch.json'), 'w'))
    os.makedirs(dropin_dir, exist_ok=True)
    tmp = managed + '.tmp'
    open(tmp, 'w').write('[Service]\n# rollout %s: %s (rollback: delete this file, daemon-reload, restart)\nEnvironment=PYTHONPATH=%s\n' %
                         (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()), a['release'], new))
    os.replace(tmp, managed)
    systemctl('daemon-reload')
    if props(unit)['effective_pythonpath'] != new:
        raise Fail('managed_dropin_not_effective', effective=props(unit)['effective_pythonpath'])
    rc, msg = systemctl('restart', unit + '.service', timeout=120)
    if rc != 0:
        raise Fail('restart_failed', detail=msg[-200:])
    ok, last = wait_healthy(unit, new, a.get('health_seconds', 90))
    if not ok:
        raise Fail('not_healthy_on_new_release', active=last.get('ActiveState'), sub=last.get('SubState'))
    return out(True, unit=unit, previous_pythonpath=current, new_pythonpath=new, main_pid=int(last['MainPID']))


def op_rollback(a):
    unit, cfg = find_unit(a['worker'])
    state_file = os.path.join(STATE_ROOT, unit, 'last-switch.json')
    if not os.path.exists(state_file):
        raise Fail('no_switch_record', unit=unit)
    rec = json.load(open(state_file))
    if rec.get('release') != a.get('release') or time.time() - rec.get('at', 0) > 7200:
        raise Fail('switch_record_is_not_for_this_rollout', recorded=rec.get('release'))   # never undo an older roll by mistake
    managed = os.path.join(UNIT_DIR, unit + '.service.d', MANAGED)
    if rec['previous_managed'] is None:
        if os.path.exists(managed):
            os.remove(managed)
    else:
        open(managed, 'w').write(rec['previous_managed'])
    systemctl('daemon-reload')
    if props(unit)['effective_pythonpath'] != rec['previous_effective']:
        raise Fail('rollback_not_effective', effective=props(unit)['effective_pythonpath'])
    if worker_busy(cfg):
        raise Fail('worker_busy')
    rc, msg = systemctl('restart', unit + '.service', timeout=120)
    if rc != 0:
        raise Fail('restart_failed', detail=msg[-200:])
    ok, last = wait_healthy(unit, rec['previous_effective'], a.get('health_seconds', 90))
    if not ok:
        raise Fail('not_healthy_after_rollback', active=last.get('ActiveState'), sub=last.get('SubState'))
    return out(True, unit=unit, restored_pythonpath=rec['previous_effective'], main_pid=int(last['MainPID']))


def op_smoke(a):
    unit, cfg = find_unit(a['worker'])
    releases = a.get('releases_dir') or DEFAULT_RELEASES
    target = os.path.join(releases, a['release'])
    _, others = release_of(props(unit)['effective_pythonpath'])
    ctx = dict(unit=unit + '.service', user_manager=True, window_s=a.get('window_s', 45), **({'modules': a['modules']} if a.get('modules') else {}),
               handlers_root=os.path.join(cfg.get('state_dir', ''), 'handler-releases'),
               release_root=target, extra_path=others, python='/usr/bin/python3')
    ctx_path = os.path.join(STATE_ROOT, unit, 'smoke-context.json')
    os.makedirs(os.path.dirname(ctx_path), exist_ok=True)
    json.dump(ctx, open(ctx_path, 'w'))
    env = dict(os.environ, PYTHONPATH=':'.join([os.path.join(target, 'node-py')] + others), PYTHONDONTWRITEBYTECODE='1')
    argv = [sys.executable, '-m', 'livestack_node.workloads.smoke', '--context', ctx_path]
    for p in a.get('probes', []):
        argv += ['--probe', p]
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=STATE_ROOT, timeout=600)
    try:
        outcome = json.loads(proc.stdout.decode().strip().splitlines()[-1])
    except Exception:
        raise Fail('smoke_unreadable', detail=proc.stderr.decode('utf-8', 'replace')[-300:])
    return out(bool(outcome.get('ok')), failure=(outcome.get('failures') or [None])[0] if not outcome.get('ok') else None,
               outcome=outcome)


OPS = {'status': op_status, 'install': op_install, 'switch': op_switch, 'rollback': op_rollback, 'smoke': op_smoke}


def main(argv):
    if len(argv) != 3 or argv[1] not in OPS:
        print('usage: host_release.py {%s} JSON' % '|'.join(sorted(OPS)), file=sys.stderr)
        return 2
    try:
        return OPS[argv[1]](json.loads(argv[2]))
    except Fail as e:
        return out(False, failure=e.failure, **e.detail)
    except Exception as e:  # never a traceback the caller has to guess at: a named failure
        return out(False, failure='host_error:%s' % type(e).__name__, detail=str(e)[:300])


if __name__ == '__main__':
    sys.exit(main(sys.argv))
