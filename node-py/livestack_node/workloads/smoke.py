"""Canary smoke probes: cheap, named checks that a rollout unit works on THIS worker.

openspec/changes/declarative-worker-rollout, D3. Each probe maps to a failure class that reached
the whole fleet before anyone noticed (2026-10-07/08):

  worker_restart_clean   unit active and not crash-looping after a restart
  handler_import         every handler script parses; no undefined or redeclared identifier
  handler_integrity      the installed bundle equals its manifest (a removed placeholder fails here)
  rootless_docker_start  rootless docker can start a container as the worker user (PrivateTmp/newuidmap)
  compilation_launch     the root-owned verifier copy is the one the unit declares
  capture_size           the runtime source capture is under its cap

Result of one probe: {probe, status, reason, elapsed_s, detail}. status is `pass`, `fail` or
`not_applicable`; a failure carries a name `smoke_failed:<probe>:<reason>`. UNKNOWN IS NOT SUCCESS:
a probe that cannot run because the host lacks the thing it checks says `not_applicable` with the
reason, never `pass`, and the runner refuses to treat a required probe as satisfied by it.

Probes are pure functions of a context dict, so each is testable against a fixture reproducing its
incident. The same module runs as a CLI on the worker (`python -m livestack_node.workloads.smoke`),
because the PrivateTmp class only shows up when the probe runs inside the worker's own sandbox.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import json
import os
import shutil
import stat
import subprocess
import symtable
import sys
import time
from pathlib import Path

PASS, FAIL, NOT_APPLICABLE = 'pass', 'fail', 'not_applicable'
MINIMUM = ('worker_restart_clean', 'handler_import', 'handler_integrity')
MAX_SOURCE_FILES = 2000
MAX_SOURCE_BYTES = 8 * 1024 * 1024
PROBE_TIMEOUT_S = 60


class Result(dict):
    pass


def _result(probe, status, reason='', started=None, **detail):
    out = Result(probe=probe, status=status, reason=reason,
                 elapsed_s=round(time.monotonic() - started, 3) if started else 0.0)
    if detail:
        out['detail'] = detail
    if status == FAIL:
        out['failure'] = f'smoke_failed:{probe}:{reason}'
    return out


def _run(argv, timeout, env=None):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return done.returncode, (done.stdout or '') + (done.stderr or '')
    except subprocess.TimeoutExpired:
        return None, 'timeout'
    except OSError as error:
        return None, f'{type(error).__name__}: {error}'


# ---- worker_restart_clean ---------------------------------------------------------------------

def worker_restart_clean(ctx):
    """ctx: unit (systemd unit name), user_manager (bool), window_s (observe time, default 60),
    systemctl (optional callable(args)->str, for tests). Pass: active/running, and neither the restart
    counter nor the main pid moved across the window."""
    started = time.monotonic()
    unit = ctx.get('unit')
    if not unit:
        return _result('worker_restart_clean', NOT_APPLICABLE, 'no_unit_named', started)
    runner = ctx.get('systemctl') or _systemctl(ctx.get('user_manager', False))

    def show():
        props = {}
        for line in runner(['show', '-p', 'ActiveState,SubState,NRestarts,MainPID,Result', unit]).splitlines():
            key, _, value = line.partition('=')
            props[key] = value
        return props
    try:
        first = show()
        if first.get('ActiveState') is None:
            return _result('worker_restart_clean', NOT_APPLICABLE, 'systemd_unavailable', started)
        time.sleep(max(0.0, float(ctx.get('window_s', 60))))
        last = show()
    except Exception as error:  # a broken systemctl is a failure to look, which is not a pass
        return _result('worker_restart_clean', FAIL, 'systemctl_error', started, error=str(error)[:200])
    if last.get('ActiveState') != 'active' or last.get('SubState') != 'running':
        return _result('worker_restart_clean', FAIL, f"not_running:{last.get('ActiveState')}/{last.get('SubState')}",
                       started, result=last.get('Result'))
    if first.get('NRestarts') != last.get('NRestarts') or first.get('MainPID') != last.get('MainPID'):
        return _result('worker_restart_clean', FAIL, 'crash_loop', started,
                       restarts=[first.get('NRestarts'), last.get('NRestarts')])
    return _result('worker_restart_clean', PASS, '', started, main_pid=last.get('MainPID'))


def _systemctl(user_manager):
    def run(args):
        code, out = _run(['systemctl', *(['--user'] if user_manager else []), *args], 15)
        if code is None:
            raise RuntimeError(out)
        return out
    return run


# ---- handler_import ---------------------------------------------------------------------------

_MODULE_DUNDERS = {'__file__', '__name__', '__doc__', '__package__', '__spec__', '__loader__',
                   '__builtins__', '__path__', '__cached__', '__class__', '__annotations__'}


def python_problems(source, filename='<handler>'):
    """Static problems a Python handler would only reveal at run time: syntax errors, a name read
    that nothing defines (NameError), and a top-level def/class defined twice (the second silently
    shadows the first). Returns a list of `kind:detail` strings."""
    try:
        tree = ast.parse(source, filename)
        top = symtable.symtable(source, filename, 'exec')
    except SyntaxError as error:
        return [f'syntax_error:{filename}:{error.lineno}:{error.msg}']
    problems = []
    if any(isinstance(n, ast.ImportFrom) and any(a.name == '*' for a in n.names) for n in ast.walk(tree)):
        return problems  # a star import hides every name; the check cannot say anything honest
    defined = set(dir(builtins)) | _MODULE_DUNDERS
    for symbol in top.get_symbols():
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace() or symbol.is_parameter():
            defined.add(symbol.get_name())

    def walk(table):
        for symbol in table.get_symbols():
            if symbol.is_declared_global() and symbol.is_assigned():
                defined.add(symbol.get_name())
        for child in table.get_children():
            walk(child)
    walk(top)
    missing = {}

    def scan(table):
        for symbol in table.get_symbols():
            if (symbol.is_global() and symbol.is_referenced() and not symbol.is_assigned()
                    and symbol.get_name() not in defined):
                missing.setdefault(symbol.get_name(), table.get_name())
        for child in table.get_children():
            scan(child)
    scan(top)
    for ident, where in sorted(missing.items()):
        problems.append(f'undefined_name:{filename}:{ident} (used in {where})')
    seen = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in seen:
                problems.append(f'redeclared:{filename}:{node.name} (lines {seen[node.name]} and {node.lineno})')
            seen[node.name] = node.lineno
    return problems


def js_problems(path, node='node'):
    """`node --check` reports syntax errors AND redeclared bindings ("Identifier 'x' has already been
    declared"). It cannot see an undefined name; eslint's no-undef does, so it runs when installed."""
    if shutil.which(node) is None:
        return None
    flag = ['--input-type=module'] if str(path).endswith('.mjs') else []
    code, out = _run([node, *flag, '--check', str(path)], 20)
    problems = []
    if code != 0:
        first = next((line for line in out.splitlines() if 'Error' in line), out.strip()[:200])
        problems.append(f'syntax_error:{Path(path).name}:{first.strip()[:200]}')
        return problems
    eslint = shutil.which('eslint')
    if eslint:
        code, out = _run([eslint, '--no-eslintrc', '--env', 'es2022,node', '--parser-options=sourceType:module',
                          '--rule', 'no-undef:error', '--rule', 'no-redeclare:error', '-f', 'unix', str(path)], 30)
        if code not in (0, None):
            problems += [f'lint:{Path(path).name}:{line[:200]}' for line in out.splitlines()
                         if 'no-undef' in line or 'no-redeclare' in line][:5]
    return problems


def _package_payloads(root, digests):
    for digest in digests:
        yield digest, Path(root) / digest / 'payload'


def handler_import(ctx):
    """ctx: handlers_root (installed package store), digests (list; default every package), node."""
    started = time.monotonic()
    root = ctx.get('handlers_root')
    if not root or not Path(root).is_dir():
        return _result('handler_import', NOT_APPLICABLE, 'no_handler_bundle', started)
    digests = ctx.get('digests') or sorted(p.name for p in Path(root).iterdir()
                                           if p.is_dir() and len(p.name) == 64)
    if not digests:
        return _result('handler_import', NOT_APPLICABLE, 'bundle_is_empty', started)
    problems, checked, unchecked = [], 0, set()
    for digest, payload in _package_payloads(root, digests):
        if not payload.is_dir():
            problems.append(f'missing_payload:{digest[:12]}')
            continue
        files = sorted(p for p in payload.rglob('*') if p.is_file() and not p.is_symlink())[:MAX_SOURCE_FILES]
        for path in files:
            suffix = path.suffix
            if suffix not in ('.py', '.js', '.mjs', '.cjs') or path.stat().st_size > MAX_SOURCE_BYTES:
                continue
            checked += 1
            label = f'{digest[:12]}/{path.relative_to(payload).as_posix()}'
            if suffix == '.py':
                problems += [f'{label}: {p}'
                             for p in python_problems(path.read_text(errors='replace'), path.name)]
            else:
                found = js_problems(path, ctx.get('node', 'node'))
                if found is None:
                    unchecked.add('javascript:no_node_binary')
                else:
                    problems += [f'{label}: {p}' for p in found]
                    if not shutil.which('eslint'):
                        unchecked.add('javascript:undefined_identifier_needs_eslint')
    if problems:
        return _result('handler_import', FAIL, problems[0][:160], started, problems=problems[:20],
                       files_checked=checked)
    if checked == 0:
        return _result('handler_import', NOT_APPLICABLE, 'no_checkable_source_files', started)
    return _result('handler_import', PASS, '', started, files_checked=checked, not_checked=sorted(unchecked))


# ---- handler_integrity ------------------------------------------------------------------------

def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def package_problems(package):
    """The installed-package rule of handler_installer.verify_installed, reported as names instead of
    raised: every file the manifest lists exists with the listed size, mode and sha256; nothing else."""
    from .handler_release import validate_manifest
    from .model import WorkloadError
    package = Path(package)
    try:
        value = json.loads((package / '.manifest.json').read_bytes())
        manifest = validate_manifest(value['manifest'], value['release_digest'])['manifest']
    except (OSError, ValueError, KeyError, TypeError, WorkloadError) as error:
        return [f'manifest_invalid:{type(error).__name__}']
    if value['release_digest'] != package.name:
        return ['identity_mismatch']
    payload = package / 'payload'
    problems, listed = [], set()
    for item in manifest['files']:
        listed.add(item['path'])
        path = payload.joinpath(*item['path'].split('/'))
        try:
            info = path.lstat()
        except OSError:
            problems.append(f"missing:{item['path']}")
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size != item['size']:
            problems.append(f"size_or_type:{item['path']}")
        elif os.name != 'nt' and stat.S_IMODE(info.st_mode) != item['mode']:
            problems.append(f"mode:{item['path']}")
        elif _sha256(path) != item['sha256']:
            problems.append(f"digest:{item['path']}")
    for base, _, names in os.walk(payload, followlinks=False):
        for entry in names:
            rel = (Path(base) / entry).relative_to(payload).as_posix()
            if rel not in listed:
                problems.append(f'unlisted:{rel}')
    return problems


def handler_integrity(ctx):
    started = time.monotonic()
    root = ctx.get('handlers_root')
    if not root or not Path(root).is_dir():
        return _result('handler_integrity', NOT_APPLICABLE, 'no_handler_bundle', started)
    digests = ctx.get('digests') or sorted(p.name for p in Path(root).iterdir()
                                           if p.is_dir() and len(p.name) == 64)
    if not digests:
        return _result('handler_integrity', NOT_APPLICABLE, 'bundle_is_empty', started)
    problems = []
    for digest in digests:
        found = package_problems(Path(root) / digest) if (Path(root) / digest).is_dir() else ['not_installed']
        problems += [f'{digest[:12]}:{p}' for p in found]
    if problems:
        return _result('handler_integrity', FAIL, problems[0][:160], started, problems=problems[:20])
    return _result('handler_integrity', PASS, '', started, packages=len(digests))


# ---- rootless_docker_start --------------------------------------------------------------------

def rootless_docker_start(ctx):
    """ctx: docker (binary), image (a locally present image), wrap (argv prefix to enter the worker's
    sandbox, e.g. ['nsenter','-t',PID,'-m']), env (extra environment), timeout_s."""
    started = time.monotonic()
    docker = ctx.get('docker') or shutil.which('docker')
    image = ctx.get('image')
    if not docker or shutil.which(docker) is None:
        return _result('rootless_docker_start', NOT_APPLICABLE, 'docker_not_installed', started)
    if not image:
        return _result('rootless_docker_start', NOT_APPLICABLE, 'no_probe_image_named', started)
    env = dict(os.environ, **(ctx.get('env') or {}))
    argv = [*(ctx.get('wrap') or []), docker, 'run', '--rm', '--pull=never', '--network=none', image,
            *(ctx.get('command') or ['true'])]
    code, out = _run(argv, ctx.get('timeout_s', 45), env)
    if code == 0:
        return _result('rootless_docker_start', PASS, '', started)
    lowered = out.lower()
    if code is None and out == 'timeout':
        reason = 'timeout'
    elif 'newuidmap' in lowered or 'newgidmap' in lowered or 'uid_map' in lowered:
        reason = 'newuidmap_denied'  # the PrivateTmp=yes class: the user-namespace helpers cannot run
    elif 'no such image' in lowered or 'unable to find image' in lowered:
        return _result('rootless_docker_start', NOT_APPLICABLE, 'probe_image_absent', started)
    elif 'cannot connect to the docker daemon' in lowered:
        reason = 'daemon_unreachable'
    else:
        reason = f'exit_{code}'
    return _result('rootless_docker_start', FAIL, reason, started, output=out.strip()[-300:])


# ---- compilation_launch -----------------------------------------------------------------------

def tree_digest(root):
    """sha256 over (relative path, file sha256) pairs: what a verifier payload or copy IS."""
    h = hashlib.sha256()
    root = Path(root)
    for path in sorted(p for p in root.rglob('*') if p.is_file() and not p.is_symlink()):
        h.update(path.relative_to(root).as_posix().encode() + b'\0' + _sha256(path).encode() + b'\n')
    return h.hexdigest()


def compilation_launch(ctx):
    """ctx: verifier_dir (the root-refreshed copy), verifier_digest (what the unit declares),
    launch_cmd (optional argv of a no-op launch). A missing or stale copy is the failure; with no
    verifier in the unit the probe is not applicable."""
    started = time.monotonic()
    expected = ctx.get('verifier_digest')
    if not expected:
        return _result('compilation_launch', NOT_APPLICABLE, 'unit_has_no_verifier', started)
    directory = ctx.get('verifier_dir')
    if not directory or not Path(directory).is_dir():
        return _result('compilation_launch', FAIL, 'verifier_copy_missing', started)
    have = tree_digest(directory)
    if have != expected:
        return _result('compilation_launch', FAIL, 'verifier_copy_stale', started,
                       have=have[:16], want=expected[:16])
    if ctx.get('launch_cmd'):
        code, out = _run(list(ctx['launch_cmd']), ctx.get('timeout_s', 45))
        if code != 0:
            return _result('compilation_launch', FAIL, 'noop_launch_failed' if code else 'noop_launch_unrunnable',
                           started, output=out.strip()[-300:])
    return _result('compilation_launch', PASS, '', started)


# ---- capture_size -----------------------------------------------------------------------------

def measure_capture(path):
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink())


def capture_size(ctx):
    """ctx: size_bytes or capture_path (measured), cap_bytes. Over the cap fails, and so does a cap
    nobody declared when a size was measured: unknown is not success."""
    started = time.monotonic()
    size = ctx.get('size_bytes')
    if size is None and ctx.get('capture_path'):
        size = measure_capture(ctx['capture_path'])
    if size is None:
        return _result('capture_size', NOT_APPLICABLE, 'no_capture_to_measure', started)
    cap = ctx.get('cap_bytes')
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        return _result('capture_size', FAIL, 'cap_undeclared', started, size_bytes=size)
    if size > cap:
        return _result('capture_size', FAIL, f'over_cap:{size}>{cap}', started, size_bytes=size, cap_bytes=cap)
    return _result('capture_size', PASS, '', started, size_bytes=size, cap_bytes=cap)


PROBES = {fn.__name__: fn for fn in (worker_restart_clean, handler_import, handler_integrity,
                                     rootless_docker_start, compilation_launch, capture_size)}


def run(names, ctx):
    """Run probes in order; never raises. Returns {ok, results, failures, not_applicable}.

    `ok` needs every named probe to pass or be not_applicable AND every MINIMUM probe to have PASSED:
    a required probe that was not applicable does not satisfy the minimum (unknown is not success)."""
    names = list(dict.fromkeys(list(MINIMUM) + [n for n in names if n not in MINIMUM]))
    unknown = [n for n in names if n not in PROBES]
    if unknown:
        raise ValueError('unknown probes: ' + ', '.join(unknown))
    results = []
    for probe in names:
        started = time.monotonic()
        try:
            results.append(PROBES[probe](ctx))
        except Exception as error:
            results.append(_result(probe, FAIL, f'probe_error:{type(error).__name__}', started,
                                   error=str(error)[:200]))
    failures = [r['failure'] for r in results if r['status'] == FAIL]
    unmet = [r['probe'] for r in results if r['probe'] in MINIMUM and r['status'] != PASS]
    return dict(ok=not failures and not unmet, results=results, failures=failures,
                not_applicable=[r['probe'] for r in results if r['status'] == NOT_APPLICABLE],
                minimum_unmet=unmet)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run rollout smoke probes on this worker')
    parser.add_argument('--probe', action='append', default=[], choices=sorted(PROBES))
    parser.add_argument('--context', required=True, help='JSON file: probe context (paths, unit name, caps)')
    args = parser.parse_args(argv)
    ctx = json.loads(Path(args.context).read_text())
    outcome = run(args.probe, ctx)
    print(json.dumps(outcome, separators=(',', ':')))
    return 0 if outcome['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
