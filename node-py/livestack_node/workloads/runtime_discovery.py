"""Find the interpreters a handler package may name as its runtime, on this host's PATH.

A worker used to advertise only what its config listed in `handler_runtimes`, so a host with
python3 installed but not listed refused every python3 handler (`handler_runtime_not_installed`)
and sat idle and eligible. Discovery fills that gap; a configured entry always wins and a
discovered one never replaces it. Every probe records its outcome — a missing or too-old
interpreter is a named reason, never silence."""
import re
import shutil
import subprocess

# runtime id -> (executable name, version regex, minimum major). `node22` means node >= 22.
RUNTIMES = {
    'python3': ('python3', r'Python (\d+)\.(\d+)', 3),
    'node22': ('node', r'v(\d+)\.(\d+)', 22),
}
PROBE_SECONDS = 5


def probe(runtime_id, which=shutil.which):
    """One runtime's outcome: {'path', 'version'} when usable, else {'reason'}."""
    executable, pattern, minimum = RUNTIMES[runtime_id]
    path = which(executable)
    if path is None:
        return {'reason': f'{executable} not found on PATH'}
    try:
        done = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=PROBE_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        return {'reason': f'{path} --version failed: {type(error).__name__}'}
    match = re.search(pattern, done.stdout + done.stderr)
    if done.returncode != 0 or match is None:
        return {'reason': f'{path} --version gave no recognised version (exit {done.returncode})'}
    if int(match.group(1)) < minimum:
        return {'reason': f'{path} is {match.group(0)}, {runtime_id} needs major >= {minimum}'}
    return {'path': path, 'version': match.group(0)}


def discover(configured, which=shutil.which):
    """(runtimes, outcomes): configured entries win; discovery adds only ids not configured."""
    runtimes = dict(configured)
    outcomes = {}
    for runtime_id in RUNTIMES:
        if runtime_id in configured:
            outcomes[runtime_id] = {'path': configured[runtime_id], 'source': 'config'}
            continue
        outcome = probe(runtime_id, which)
        outcomes[runtime_id] = dict(outcome, source='discovered')
        if 'path' in outcome:
            runtimes[runtime_id] = outcome['path']
    return runtimes, outcomes
