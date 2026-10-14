#!/usr/bin/env bash
# Build the release directory that tools/deploy-authority-release.sh deploys.
#
#   tools/build-authority-release.sh <commit> <current-release-dir>
#
# A release is  <dir>/node-py/{livestack_node/, LICENSE, RELEASE.json, _deps/}:
#   - livestack_node/  the package exactly as at <commit> (git archive; never the working tree)
#   - LICENSE, _deps/  copied unchanged from the release now live (dependencies change rarely;
#                      to change them, build _deps by hand and say so in the deploy record)
#   - RELEASE.json     commit, file count, and a sha256 over (path, contents) of every file
#                      outside _deps and __pycache__ (the hash scheme of older releases is
#                      undocumented; this one is reproducible from the directory alone)
# (Release livestack-2a2f3ee6 was built by hand before this script; its content_hash used "./"-prefixed paths and differs from this script's.)
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
python3 - "$DEST/node-py" "$COMMIT" <<'PY'
import hashlib, json, os, sys
root, commit = sys.argv[1:]
os.chdir(root)
files = sorted(os.path.join(d, f)[2:] for d, ds, fs in os.walk('.') if not d.startswith('./_deps') and d != './_deps'
               for f in fs if f != 'RELEASE.json')
h = hashlib.sha256()
for f in files:
    h.update(f.encode() + b'\0' + open(f, 'rb').read() + b'\0')
json.dump({"commit": commit, "content_hash": h.hexdigest(), "files": len(files) + 1}, open('RELEASE.json', 'w'), indent=1)
PY
echo "built $DEST"
