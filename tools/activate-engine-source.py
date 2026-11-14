#!/usr/bin/env python3
"""Run the reviewed Harmony engine-source operator command from this checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'node-py'))
from livestack_node.engine_activation import main

if __name__ == '__main__':
    raise SystemExit(main())
