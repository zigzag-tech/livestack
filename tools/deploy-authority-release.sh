#!/usr/bin/env bash
# Deploy a candidate release directory to the live workload authority, with a rollback.
#
#   tools/deploy-authority-release.sh <release-dir> [candidate-config.json]
#
# Order is the procedure (see _plans/durable-workloads.md, "Authority release deployment"):
#   1. tools/check-authority-release.py on the candidate (static + throwaway authority + a real worker
#      registration + a job round trip). A FAIL stops here; nothing live has been touched.
#   2. Back up the authority database and config (verified).
#   3. Point the unit at the candidate through a drop-in, optionally install the candidate config,
#      restart.
#   4. Verify the LIVE authority: status answers AND at least as many workers are ready as before the
#      restart (worker registration is what a missing import breaks while status still answers).
#   5. Any failure after step 2 rolls back: drop-in removed, config restored, restart.
# Arguments, not environment variables: the unit and paths below are this deployment's defaults.
set -uo pipefail
RELEASE=${1:?usage: deploy-authority-release.sh <release-dir> [candidate-config.json]}
CANDIDATE_CONFIG=${2:-}
UNIT=livestack-workload-authority
CFG=$HOME/.config/livestack-workloads/authority.json
STATE=$HOME/.local/state/livestack-workloads/authority
DROPIN_DIR=$HOME/.config/systemd/user/$UNIT.service.d
# Release overlays use a z-prefixed filename; keep this stable override later
# than versioned overlays so systemd cannot keep selecting an older PYTHONPATH.
DROPIN=$DROPIN_DIR/zzzzzzzzzzzzzzzz-release-current.conf
HERE=$(cd "$(dirname "$0")" && pwd)
NODE_PY=$RELEASE/node-py; [ -d "$NODE_PY" ] || NODE_PY=$RELEASE
PYPATH=$NODE_PY:$NODE_PY/_deps
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=$HOME/.local/state/livestack-workloads/backups/release-$STAMP

echo "== 1. check candidate"
python3 "$HERE/check-authority-release.py" "$RELEASE" || { echo "ABORT: candidate failed its check; live authority untouched"; exit 2; }

ready_workers() {  # prints "<fresh_ready> <fresh>" from the live authority, or exits non-zero
  PYTHONPATH=$PYPATH PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 - "$CFG" <<'PY'
import json, sys, time
from pathlib import Path
from livestack_node.workloads.client import WorkloadClient
cfg = json.loads(Path(sys.argv[1]).read_text())
admin = next(p for p in cfg["principals"] if p["role"] == "admin")
status = WorkloadClient("http://%s:%s" % (cfg.get("bind", "127.0.0.1"), cfg.get("port", 8802)), admin["token"]).request("handler-releases/status")
now = time.time(); fresh = [w for w in status["workers"] if now - w["seen"] < 120]
print(sum(1 for w in fresh if w["ready"]), len(fresh))
PY
}

echo "== 2. back up"
mkdir -p "$BACKUP" && chmod 700 "$BACKUP"
if ! /usr/bin/python3 - "$STATE/workloads.sqlite" "$BACKUP/workloads.sqlite" <<'PY'
import os, sqlite3, sys
from pathlib import Path

source_path = Path(sys.argv[1]).resolve(strict=True)
backup_path = Path(sys.argv[2])
source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
backup = sqlite3.connect(backup_path)
try:
  source.backup(backup)
  if backup.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
    raise RuntimeError('backup integrity_check failed')
finally:
  backup.close()
  source.close()
os.chmod(backup_path, 0o600)
print('backup integrity_check: ok')
PY
then
  rm -f "$BACKUP/workloads.sqlite"
  echo "ABORT: backup failed"; exit 2
fi
install -m 600 "$CFG" "$BACKUP/authority.json"
[ -f "$DROPIN" ] && cp -p "$DROPIN" "$BACKUP/zz-release-current.conf.previous"
read -r BEFORE_READY BEFORE_FRESH < <(ready_workers 2>/dev/null || echo "0 0")
echo "   backup: $BACKUP   workers ready before: $BEFORE_READY of $BEFORE_FRESH fresh"

rollback() {
  echo "ROLLBACK: $1"
  if [ -f "$BACKUP/zz-release-current.conf.previous" ]; then cp -p "$BACKUP/zz-release-current.conf.previous" "$DROPIN"; else rm -f "$DROPIN"; fi
  install -m 600 "$BACKUP/authority.json" "$CFG"
  systemctl --user daemon-reload; systemctl --user restart $UNIT; sleep 3
  echo "   unit: $(systemctl --user is-active $UNIT)"; exit 1
}

live_release_path_matches() {
  local pid
  pid=$(systemctl --user show --property=MainPID --value "$UNIT") || return 1
  [ "$pid" -gt 1 ] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" | grep -Fxq "PYTHONPATH=$PYPATH"
}

echo "== 3. switch and restart"
mkdir -p "$DROPIN_DIR"
printf '[Service]\n# %s: release %s. Roll back: delete this file (or restore %s/zz-release-current.conf.previous), restore %s/authority.json, daemon-reload, restart.\nEnvironment=PYTHONPATH=%s\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n' \
  "$STAMP" "$RELEASE" "$BACKUP" "$BACKUP" "$PYPATH" > "$DROPIN"
[ -n "$CANDIDATE_CONFIG" ] && install -m 600 "$CANDIDATE_CONFIG" "$CFG"
systemctl --user daemon-reload
t0=$(date +%s); systemctl --user restart $UNIT || rollback "restart command failed"

echo "== 4. verify live"
for i in $(seq 1 90); do
  if live_release_path_matches && read -r READY FRESH < <(ready_workers 2>/dev/null) && [ "$READY" -ge "$BEFORE_READY" ] && [ "$READY" -gt 0 ]; then
    echo "   healthy $(( $(date +%s) - t0 ))s after restart: workers ready $READY of $FRESH (before: $BEFORE_READY)"; echo "DEPLOYED $RELEASE"; exit 0
  fi
  sleep 2
done
journalctl --user -u $UNIT -n 12 --no-pager 2>&1 | sed -E 's/(token|secret)[^ ]*/<redacted>/Ig' | tail -12
rollback "after 180s status/worker readiness did not recover (ready ${READY:-?} < before $BEFORE_READY)"
