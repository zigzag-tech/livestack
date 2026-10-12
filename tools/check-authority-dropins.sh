#!/usr/bin/env bash
# Refuse a deploy that a later drop-in would silently undo.
#
#   tools/check-authority-dropins.sh <dropin-dir> <deploy-dropin-file-name>
#
# systemd applies drop-ins in lexical order of file name and the LAST Environment=PYTHONPATH wins. The
# deploy script writes <deploy-dropin-file-name>; any other *.conf that sorts after it and sets PYTHONPATH
# makes the deploy a no-op after a full authority restart (the verify step then rolls back). It happened
# twice (zzzzzzzzzzzzzzzzz-release-roster-verdict.conf). Exit 0 = clean, 1 = overriding file(s) named.
set -uo pipefail
dir=${1:?usage: check-authority-dropins.sh <dropin-dir> <deploy-dropin-file-name>}
mine=${2:?usage: check-authority-dropins.sh <dropin-dir> <deploy-dropin-file-name>}
export LC_ALL=C   # systemd sorts bytewise; the shell's > must too
bad=()
shopt -s nullglob
for f in "$dir"/*.conf; do
  n=$(basename "$f")
  if [[ "$n" > "$mine" ]] && grep -Eq '^[[:space:]]*Environment=.*PYTHONPATH=' "$f"; then bad+=("$n"); fi
done
if [ ${#bad[@]} -gt 0 ]; then
  echo "FAIL: ${bad[*]} sorts after $mine and sets PYTHONPATH, so it would override the release this deploy installs."
  echo "      Rename it to end in .superseded (or delete it) first; keep a copy in the backup directory."
  exit 1
fi
echo "PASS: no drop-in after $mine sets PYTHONPATH"
