"""Apply the `units_probe` config key (process_node.probe_units) to an older facade release:
`python3 backport-process-node-units-probe.py <release>/process_node.py` edits it in place and refuses
(AssertionError) if the file does not have a shape this was written for.

`probe_units` is copied verbatim from this checkout's livestack_node/process_node.py, so the release
gets exactly main's function; the file also gains the imports it needs and one call in `main()`.
See node-py/docs/process-node-units-probe.md. Used 2026-11-23 on the polytts facades of node-g
(base e2fb4bbace24e144) and node-h (base e2fb4bbace24e144-holds-stale)."""
from pathlib import Path
import sys

p = sys.argv[1]
s = open(p).read()
main_src = (Path(__file__).resolve().parent.parent / 'livestack_node' / 'process_node.py').read_text()
start = main_src.index('UNITS_PROBE_TIMEOUT_S = 30\n')
block = main_src[start:main_src.index('class ProcessNode:\n', start)]


def rep(a, b, count=1):
    global s
    n = s.count(a)
    assert n == count, (a, n)
    s = s.replace(a, b)


assert 'def probe_units' not in s, 'already applied'
rep('import signal\nimport sys\n', 'import signal\nimport subprocess\nimport sys\n')
if 'import logging\n' not in s:
    rep('import json\nimport os\n', 'import json\nimport logging\nimport os\n')
if "log = logging.getLogger('livestack.process_node')" not in s:
    rep('\nclass ProcessNode:\n', "\nlog = logging.getLogger('livestack.process_node')\n\n\nclass ProcessNode:\n")
rep('\nclass ProcessNode:\n', '\n' + block + 'class ProcessNode:\n')
rep("""        raise RuntimeError('process_node_cpu_interpreter_required')
""", """        raise RuntimeError('process_node_cpu_interpreter_required')
    config=probe_units(config)
""")
compile(s, p, 'exec')
open(p, 'w').write(s)
