#!/usr/bin/env bash
# Build the release directory that tools/deploy-authority-release.sh deploys.
#
#   tools/build-authority-release.sh <commit> <current-release-dir>
#
# A release is  <dir>/node-py/{livestack_node/, LICENSE, RELEASE.json, _deps/}:
#   - livestack_node/  the package exactly as at <commit> (git archive; never the working tree)
#   - LICENSE, _deps/  copied unchanged from the release now live (dependencies change rarely;
#                      to change them, build _deps by hand and say so in the deploy record)
#   - RELEASE.json     commit, file count, and content_hash = the worker-release hash
#                      (node-py/livestack_node/workloads/host_release.py content_hash, the algorithm of
#                      build-worker-release.py: every file under node-py/ including _deps, except RELEASE.json,
#                      __pycache__, *.pyc and symlinks). A release built here can therefore also be staged
#                      on workers and named in a deployment unit; with the former scheme (_deps excluded)
#                      the executor refused it as staged_content_hash_mismatch (livestack-fc8b905a, 2026-10-13).
# (Releases built before 2026-10-15, e.g. 2a2f3ee6, 123818dd and fc8b905a, carry the older hash in RELEASE.json
# and are not worker-compatible: rebuild with build-worker-release.py before naming them in a unit.)
# Output: ~/.local/share/livestack-workload-releases/livestack-<8-char commit>. Refuses to overwrite.
# Then: python3 tools/check-authority-release.py <dir>; tools/deploy-authority-release.sh <dir>
# (the deploy script runs the check itself). Run it from a clone that has <commit> fetched.
set -euo pipefail
COMMIT=$(git rev-parse --verify "${1:?usage: build-authority-release.sh <commit> <current-release-dir>}^{commit}")
OLD=${2:?usage: build-authority-release.sh <commit> <current-release-dir>}

OLD=$(cd "$OLD" && pwd); [ -d "$OLD/node-py" ] && OLD=$OLD/node-py
DEST=$HOME/.local/share/livestack-workload-releases/livestack-${COMMIT:0:8}
[ ! -e "$DEST" ] || { echo "refusing: $DEST exists" >&2; exit 1; }
mkdir -p "$DEST" && git archive "$COMMIT" node-py/livestack_node | tar -x -C "$DEST"
cp "$OLD/LICENSE" "$DEST/node-py/LICENSE"; cp -a "$OLD/_deps" "$DEST/node-py/_deps"
find "$DEST" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
PYTHONPATH="$(cd "$(dirname "$0")/.." && pwd)/node-py" python3 - "$DEST/node-py" "$COMMIT" <<'PY'
import json, os, sys
from livestack_node.workloads.host_release import content_hash
root, commit = sys.argv[1:]
files = sum(len(fs) for _, _, fs in os.walk(root))
json.dump({"commit": commit, "content_hash": content_hash(os.path.dirname(root)), "files": files + 1}, open(os.path.join(root, 'RELEASE.json'), 'w'), indent=1)
PY
echo "built $DEST"
