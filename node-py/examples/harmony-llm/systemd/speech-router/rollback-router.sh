#!/bin/bash
# Roll router CODE back. With no argument: restore the newest env.bak.<ts> (which names the previous
# HARMONY_ROUTER_REV) and point `current` at that release. With <rev>: point `current` at releases/<rev>
# and set only HARMONY_ROUTER_REV. Quiet check, restart, verify /health. Does not touch the relay target
# (rollback.sh does that). DRY_RUN=1 skips the quiet check, restart and health probe.
set -euo pipefail
B=${ROUTER_BASE:-$HOME/.local/share/harmony-speech-router}
ENVF=${ROUTER_ENV:-$HOME/.config/harmony-speech-router/env}
UNIT=${ROUTER_UNIT:-harmony-speech-router.service}
PORT=${ROUTER_PORT:-8189}
DRY=${DRY_RUN:-0}
ts=$(date -u +%Y%m%dT%H%M%S.%NZ)
if [ -n "${1:-}" ]; then
  rev=$1; [ -d "$B/releases/$rev" ] || { echo "no release $rev"; exit 1; }
  cp -p "$ENVF" "$ENVF.bak.$ts"; chmod 600 "$ENVF.bak.$ts"
  if grep -q '^HARMONY_ROUTER_REV=' "$ENVF"; then sed -i "s|^HARMONY_ROUTER_REV=.*|HARMONY_ROUTER_REV=$rev|" "$ENVF"; else echo "HARMONY_ROUTER_REV=$rev" >> "$ENVF"; fi
else
  bak=$(ls -1 "$ENVF".bak.* 2>/dev/null | sort | tail -1); [ -n "$bak" ] || { echo "no env backup"; exit 1; }
  rev=$(sed -n 's/^HARMONY_ROUTER_REV=//p' "$bak"); [ -d "$B/releases/$rev" ] || { echo "backup names rev '$rev', no such release"; exit 1; }
  cp -p "$ENVF" "$ENVF.bak.$ts"; cp -p "$bak" "$ENVF"; echo "env restored from $bak"
fi
chmod 600 "$ENVF"
ln -sfn "$B/releases/$rev" "$B/current.new" && mv -T "$B/current.new" "$B/current"; echo "current -> releases/$rev"
if [ "$DRY" = 1 ]; then echo "dry run: no quiet check, restart or health probe"; exit 0; fi
"$B/relay-quiet.sh" 480 || { echo "relay never went quiet in 8 min: NOT restarting (code and env are switched)"; exit 2; }
systemctl --user restart "$UNIT"; sleep 3
for _ in 1 2 3 4 5; do
  got=$(curl -s "http://127.0.0.1:$PORT/health" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("revision",""))' 2>/dev/null || true)
  [ "$got" = "$rev" ] && { echo "health reports rev $got: rolled back"; exit 0; }; sleep 2
done
echo "health did not report $rev (got '${got:-nothing}')"; exit 3
