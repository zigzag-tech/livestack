#!/usr/bin/env python3
"""Conformance scan: handler modules may not reach raw process, network or sleep primitives.

    check-supervised-handlers.py [config.json]      (default: tools/supervised-handlers.json)

Config: {"handlers": [glob, ...], "allowlist": [path, ...], "max_allowlist": N}. Paths and globs are
relative to the config file's repository root (its parent's parent). The allowlist is a ratchet like the
file-size one: it may shrink, never grow (`max_allowlist` is the committed ceiling and lowering it is
the only direction that needs no review); an entry that no longer violates is stale and fails the scan.
Every handler module must also actually use `supervised(`. The ctx implementation itself
(workloads/supervised/supervised.{py,mjs}) is never scanned: it is where the primitives live.
Exit 0 clean, 1 violations."""
import glob
import json
import re
import sys
from pathlib import Path

PY = [r'^\s*(import|from)\s+(subprocess|requests|httpx|aiohttp|socket|http|urllib|ftplib|smtplib|telnetlib|'
      r'multiprocessing|pty|importlib|ctypes)\b', r'\btime\s*\.\s*sleep\b', r'\bfrom\s+time\s+import\b[^\n]*\bsleep\b',
      r'\basyncio\s*\.\s*(sleep|create_subprocess\w*)\b', r'\bos\s*\.\s*(system|popen|spawn\w*|exec\w*|fork)\b',
      r'\b__import__\b', r'(?<![\w.])(eval|exec)\s*\(']
JS = [r'child_process', r'\bfetch\s*\(', r'node:(https?|net|dns|tls|dgram|http2|worker_threads|vm|cluster)\b',
      r'\b(setTimeout|setInterval|setImmediate)\s*\(', r'timers/promises', r'\bXMLHttpRequest\b', r'\bWebSocket\b',
      r'\brequire\s*\(', r'\bimport\s*\(', r'(?<![\w.])eval\s*\(', r'new\s+Function\b', r'Atomics\.wait', r'process\.binding']
IMPLEMENTATION = {'supervised.py', 'supervised.mjs'}


def violations(path):
    rules = PY if path.suffix == '.py' else JS
    found = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(('#', '//', '*', '/*')):
            continue
        for rule in rules:
            match = re.search(rule, line)
            if match:
                found.append(f'{path}:{number}: {match.group(0).strip()!r} in: {stripped[:80]}')
    if 'supervised(' not in path.read_text():
        found.append(f'{path}: handler module does not use supervised(')
    return found


def scan(config_path):
    config_path = Path(config_path).resolve()
    root = config_path.parent.parent
    config = json.loads(config_path.read_text())
    allow = set(config.get('allowlist', []))
    problems = []
    if len(allow) > config.get('max_allowlist', 0):
        problems.append(f'allowlist has {len(allow)} entries, ceiling is {config.get("max_allowlist", 0)}: it may only shrink')
    modules = sorted({Path(p) for pattern in config['handlers'] for p in glob.glob(str(root/pattern), recursive=True)
                      if Path(p).suffix in ('.py', '.mjs') and Path(p).name not in IMPLEMENTATION})
    for module in modules:
        relative = str(module.relative_to(root))
        found = violations(module)
        if relative in allow:
            if not found:
                problems.append(f'{relative}: allowlisted but clean; remove it from the allowlist')
            allow.discard(relative)
        else:
            problems.extend(found)
    problems.extend(f'{entry}: allowlist entry matches no handler module' for entry in sorted(allow))
    return modules, problems


if __name__ == '__main__':
    target = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).with_name('supervised-handlers.json'))
    _, problems = scan(target)
    for problem in problems:
        print(problem)
    sys.exit(1 if problems else 0)
