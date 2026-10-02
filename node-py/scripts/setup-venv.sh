#!/usr/bin/env bash
# Create (or repair) node-py/.venv — the interpreter the Harmony broker units
# run (`livestack-buildd`, `.venv/bin/python -m livestack_node.hostd`).
#
#   node-py/scripts/setup-venv.sh            # broker deps (fastapi + uvicorn)
#   node-py/scripts/setup-venv.sh dev        # extras to install, comma-separated
#
# The venv is machine-local and gitignored. It was once committed by accident
# (54b5c6b5) and untracked with a plain `git rm` (b0c2b78f), which DELETED
# bin/ and pyvenv.cfg from every checkout that pulled it; zz-tower2's
# livestack-buildd kept running on the open interpreter until its next restart
# (2026-09-29), then crash-looped on 203/EXEC for days. This script is the one
# documented way to recreate it. Idempotent: a venv missing its interpreter is
# rebuilt with --clear; an intact one is updated in place.
set -euo pipefail

extras="${1:-broker}"
cd "$(dirname "$0")/.."
venv=.venv

if [ -d "$venv" ] && [ ! -x "$venv/bin/python" ]; then
  echo "setup-venv: $PWD/$venv exists without an interpreter; rebuilding it" >&2
fi
if [ ! -x "$venv/bin/python" ]; then
  python3 -m venv --clear "$venv"
fi
"$venv/bin/python" -m pip install -q --upgrade pip
"$venv/bin/python" -m pip install -q -e ".[${extras}]"
"$venv/bin/python" -c "import livestack_node.hostd, fastapi, uvicorn"
echo "setup-venv: $PWD/$venv ready (extras: ${extras})"
