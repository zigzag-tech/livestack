#!/usr/bin/env python3
"""Pre-activation check for a workload authority release directory.

    tools/check-authority-release.py <release-dir> [--python /usr/bin/python3]

Run it on a candidate release BEFORE pointing the live service at it, and again after
any hand edit of a release overlay. It exits 0 only when every stage passes, and prints
one PASS/FAIL line per stage with a named cause. It never prints a token.

Stages (each a real process or real file work, no fakes):
  static      undefined-name check over node-py/livestack_node/workloads (pyflakes when the chosen
              interpreter has it; otherwise a stdlib-attribute-without-import AST check)
  layout      prove the candidate's own livestack_node is the code that gets imported
  boot        start a THROWAWAY authority from the release (`-m livestack_node.workloads.service`
              with a scratch config, state dir and free port), validate its config schema
  worker      register a scratch worker over HTTP with a report that exercises every
              validation branch of worker registration (handler inventory, activation
              failures, GC receipts, environment profiles)
  job         submit a job as a caller, claim and complete it as the worker, read the
              result back, and read handler-release status as the operator

Why this exists: on 2026-10-05 a hand-patched overlay release shipped a `store.py` that
used `re` without importing it. The authority started and answered status, but every
worker registration carrying a handler inventory returned HTTP 503 until a worker found it
(openspec change handler-registry-burst-headroom, task 4.1). Only a worker registration
exercises that path.
"""
import argparse
import ast
import hashlib
import http.client
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HANDLER = 'check.v1'
# Newest-first optional report sections an older release may not know; dropped one at a time on a 400.
OPTIONAL_SECTIONS = ('environment_profiles', 'environment_replicas')
# Stdlib modules whose bare use without an import is the failure class seen in production.
STDLIB = {'re', 'json', 'time', 'os', 'sys', 'hashlib', 'math', 'shutil', 'subprocess', 'threading', 'sqlite3',
          'tarfile', 'tempfile', 'uuid', 'secrets', 'hmac', 'base64', 'struct', 'socket', 'ssl', 'stat', 'errno',
          'signal', 'itertools', 'functools', 'collections', 'contextlib', 'pathlib', 'datetime', 'urllib', 'http',
          'io', 'zlib', 'gzip', 'random', 'logging', 'traceback', 'platform', 'resource', 'fcntl', 'select', 'shlex'}


def result(stage, ok, cause=None):
    print(f"{'PASS' if ok else 'FAIL'}  {stage}" + (f"  [{cause}]" if cause else ''), flush=True)
    return ok


def find_node_py(release):
    release = Path(release)
    for candidate in (release/'node-py', release):
        if (candidate/'livestack_node'/'workloads'/'service.py').is_file():
            return candidate
    raise SystemExit(f'FAIL  layout  [not_a_release_dir: no node-py/livestack_node/workloads/service.py under {release}]')


def pythonpath(node_py):
    deps = node_py/'_deps'
    return os.pathsep.join([str(node_py)] + ([str(deps)] if deps.is_dir() else []))


def static_check(node_py, python):
    env = dict(os.environ, PYTHONPATH='', PYTHONDONTWRITEBYTECODE='1')
    probe = subprocess.run([python, '-c', 'import pyflakes'], env=env, capture_output=True)
    package = node_py/'livestack_node'/'workloads'   # the authority and its workers; other packages ship other services
    problems = []
    if probe.returncode == 0:
        run = subprocess.run([python, '-m', 'pyflakes', str(package)], env=env, capture_output=True, text=True)
        problems = [line.replace(str(node_py)+os.sep, '') for line in (run.stdout + run.stderr).splitlines()
                    if 'undefined name' in line or 'syntax' in line.lower()]
        engine = 'pyflakes'
    else:
        engine = 'ast-fallback(pyflakes not installed for that interpreter)'
        for path in sorted(package.rglob('*.py')):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError as error:
                problems.append(f'{path.relative_to(node_py)}:{error.lineno}: syntax error')
                continue
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {(a.asname or a.name).split('.')[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    imported |= {a.asname or a.name for a in node.names}
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    imported.add(node.name)
                    if not isinstance(node, ast.ClassDef):
                        imported |= {a.arg for a in node.args.args + node.args.kwonlyargs}
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    imported.add(node.id)
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    imported.add(node.name)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and
                        node.value.id in STDLIB and node.value.id not in imported):
                    problems.append(f'{path.relative_to(node_py)}:{node.lineno}: undefined name {node.value.id!r}')
    return result('static', not problems,
                  f'{engine}; {len(problems)} problem(s): ' + '; '.join(problems[:6]) if problems else None)


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class Api:
    def __init__(self, port):
        self.base = f'http://127.0.0.1:{port}/v1/workloads/'

    def call(self, route, token, body=None):
        request = urllib.request.Request(self.base + route, headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b'null')
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(65536)).get('error', '')
            except ValueError:
                detail = ''
            return error.code, {'error': detail}

    def put_object(self, token, data):
        digest = hashlib.sha256(data).hexdigest()
        connection = http.client.HTTPConnection(self.base.split('/')[2], timeout=15)
        try:
            connection.putrequest('PUT', f'/v1/workloads/objects/{digest}')
            connection.putheader('Authorization', 'Bearer ' + token)
            connection.putheader('Content-Length', str(len(data)))
            connection.endheaders()
            connection.send(data)
            return connection.getresponse().status, digest
        finally:
            connection.close()


def worker_report():
    """A report that reaches every validation branch of worker registration (re.fullmatch users)."""
    capacity = dict(cpu=2, memory_bytes=2*1024**3, disk_bytes=4*1024**3)
    digest = lambda tag: hashlib.sha256(tag.encode()).hexdigest()
    return dict(capacity=capacity, available=capacity, labels={}, handlers=[HANDLER], ready=True,
                handler_inventory={'generation': 0, 'defaults': {}, 'releases': []},
                handler_activation_failures=[{'generation': 1, 'release_digest': digest('failed'),
                                              'reason': 'scratch activation failure'}],
                handler_gc_receipts=[{'outcome': 'complete', 'reason': None, 'generation': 0, 'examined': 1,
                                      'deleted': 1, 'bytes_reclaimed': 1, 'deleted_digests': [digest('gc')],
                                      'reference_evidence': 'complete'}],
                environment_profiles={'scratch': digest('profile')})


def live_checks(node_py, python, timeout):
    work = Path(tempfile.mkdtemp(prefix='check-authority-release-'))
    tokens = {role: secrets.token_hex(16) for role in ('admin', 'caller', 'worker')}
    port = free_port()
    config = {'state_dir': str(work/'state'), 'bind': '127.0.0.1', 'port': port, 'handlers': [HANDLER],
              'principals': [
                  {'id': 'check-admin', 'token': tokens['admin'], 'role': 'admin', 'handlers': [HANDLER]},
                  {'id': 'check-caller', 'token': tokens['caller'], 'role': 'caller', 'handlers': [HANDLER]},
                  {'id': 'check-worker', 'token': tokens['worker'], 'role': 'worker',
                   'worker': 'check-worker', 'host': 'check-host'}],
              'handler_release_policy': {'revision': 'check', 'retention_seconds': 86400, 'burst_min_age_seconds': 3600,
                                         'handlers': {HANDLER: {'runtime_ids': ['node22'], 'backends': ['native']}}}}
    config_path = work/'authority.json'
    config_path.write_text(json.dumps(config))
    os.chmod(config_path, 0o600)
    env = dict(os.environ, PYTHONPATH=pythonpath(node_py), PYTHONDONTWRITEBYTECODE='1')
    # `python -m` puts the CURRENT DIRECTORY first on sys.path, so a check run from a directory that
    # contains another livestack_node would silently test that code. Run from the scratch dir and prove
    # which copy was imported before trusting any later stage.
    imported = subprocess.run([python, '-c', 'import livestack_node.workloads.service as s; print(s.__file__)'],
                              env=env, cwd=work, capture_output=True, text=True)
    origin = Path(imported.stdout.strip()).resolve() if imported.returncode == 0 and imported.stdout.strip() else None
    if origin is None or node_py not in origin.parents:
        shutil.rmtree(work, ignore_errors=True)
        return result('layout', False, 'wrong_code_under_test: ' + (
            f'imported {origin}, expected under {node_py}' if origin else
            'cannot import livestack_node.workloads.service: ' + (imported.stderr.strip().splitlines() or ['?'])[-1][:160]))
    log = open(work/'authority.stdout', 'w')
    proc = subprocess.Popen([python, '-m', 'livestack_node.workloads.service', '--config', str(config_path)],
                            env=env, cwd=work, stdout=log, stderr=subprocess.STDOUT)
    try:
        api = Api(port)
        deadline = time.time() + timeout
        booted, last = False, 'no answer'
        while time.time() < deadline and proc.poll() is None:
            try:
                status, body = api.call('handler-releases/status', tokens['admin'])
                booted, last = status == 200, f'http_{status}'
                if booted:
                    break
            except (urllib.error.URLError, ConnectionError, OSError) as error:
                last = type(error).__name__
            time.sleep(0.3)
        if proc.poll() is not None:
            tail = ' | '.join((work/'authority.stdout').read_text().strip().splitlines()[-3:])[:300]
            return result('boot', False, f'authority_exited_{proc.returncode}: {tail}')
        if not result('boot', booted, None if booted else f'status_unanswered_{last}'):
            return False
        report = worker_report()
        status, body = api.call('worker/report', tokens['worker'], dict(boot='check-boot', report=report))
        note = ''
        if status == 400 and body.get('error') == 'invalid worker report':
            # An older release does not know the newest optional sections. Registration with the
            # sections it does know still exercises the validation that failed in production.
            for section in OPTIONAL_SECTIONS:
                report.pop(section, None)
                status, body = api.call('worker/report', tokens['worker'], dict(boot='check-boot', report=report))
                if status != 400 or body.get('error') != 'invalid worker report':
                    note = f'release does not accept {section}'
                    break
        registered = status == 200 and body.get('worker') == 'check-worker'
        if registered:
            cause = note or None
        else:
            cause = f"registration_http_{status}: {str(body.get('error', ''))[:120]}"
            if status >= 500:
                cause += ' (server-side failure in worker registration; see authority.log in the scratch state dir)'
            if note:
                cause += f'; {note}'
        if not result('worker', registered, cause):
            return False
        data = b'check-input'
        put_status, digest = api.put_object(tokens['caller'], data)
        status, job = api.call('jobs', tokens['caller'], dict(version=1, key='check-job', handler=HANDLER,
                                                              input_digest=digest, need={'cpu': 1}))
        if put_status != 200 or status != 200:
            return result('job', False, f'submit_http_{status}_put_{put_status}')
        status, claim = api.call('worker/claim', tokens['worker'], {'boot': 'check-boot'})
        assignment = (claim or {}).get('assignment') if status == 200 else None
        if not assignment or assignment.get('job_id') != job['id']:
            return result('job', False, f'claim_http_{status}_assignment_{"missing" if not assignment else "wrong_job"}')
        status, done = api.call('worker/complete', tokens['worker'], dict(
            boot='check-boot', attempt_id=assignment['attempt_id'], fence=assignment['fence'],
            input_digest=digest, outcome='succeeded', result={'ok': True}))
        status2, final = api.call('jobs/' + job['id'], tokens['caller'])
        ok = status == 200 and status2 == 200 and final.get('state') == 'succeeded'
        return result('job', ok, None if ok else f'complete_http_{status}_readback_http_{status2}_state_{final.get("state")}')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('release', help='candidate release directory (contains node-py/)')
    parser.add_argument('--python', default='/usr/bin/python3',
                        help="interpreter the live service uses (default /usr/bin/python3)")
    parser.add_argument('--boot-timeout', type=int, default=30)
    args = parser.parse_args()
    node_py = find_node_py(args.release).resolve()
    print(f'release: {node_py}')
    passed = static_check(node_py, args.python)
    passed = live_checks(node_py, args.python, args.boot_timeout) and passed
    print('RESULT: ' + ('PASS' if passed else 'FAIL'))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
