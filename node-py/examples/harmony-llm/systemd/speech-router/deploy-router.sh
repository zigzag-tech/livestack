#!/bin/bash
# Deploy router CODE <rev> (a livestack commit that exists in $ROUTER_SRC_REPO): releases/<rev8>/node-py,
# repoint `current`, set HARMONY_ROUTER_REV + HARMONY_ROUTER_PUBLIC_ERRORS in the env file (backup
# env.bak.<ts>, mode 600), wait for a quiet relay, restart the user unit, verify /health reports the rev.
# apply.sh / rollback.sh only touch the relay target; they never move router code.
# DRY_RUN=1 does everything except the quiet check, the restart and the health probe (use it with
# ROUTER_BASE / ROUTER_ENV pointed at a throwaway directory). Prints no value from the env file.
set -euo pipefail
rev_in=${1:?usage: deploy-router.sh <livestack-rev>}
B=${ROUTER_BASE:-$HOME/.local/share/harmony-speech-router}
ENVF=${ROUTER_ENV:-$HOME/.config/harmony-speech-router/env}
SRC=${ROUTER_SRC_REPO:-$HOME/livestack}
UNIT=${ROUTER_UNIT:-harmony-speech-router.service}
PORT=${ROUTER_PORT:-8189}
DRY=${DRY_RUN:-0}
rev=$(git -C "$SRC" rev-parse --verify --short=8 "$rev_in^{commit}")
R=$B/releases/$rev
if [ -d "$R" ]; then echo "release $rev already present"; else
  tmp=$(mktemp -d "$B/releases/.new.XXXXXX"); mkdir -p "$B/releases"
  git -C "$SRC" archive "$rev" node-py/livestack_node node-py/examples | tar -x -C "$tmp"
  mv "$tmp" "$R"; echo "release $rev extracted"
fi
[ -f "$ENVF" ] || { echo "no env file at $ENVF"; exit 1; }
ts=$(date -u +%Y%m%dT%H%M%S.%NZ); cp -p "$ENVF" "$ENVF.bak.$ts"; chmod 600 "$ENVF" "$ENVF.bak.$ts"
echo "env backup: $ENVF.bak.$ts"
setvar() { if grep -q "^$1=" "$ENVF"; then sed -i "s|^$1=.*|$1=$2|" "$ENVF"; else echo "$1=$2" >> "$ENVF"; fi; }
setvar HARMONY_ROUTER_REV "$rev"; setvar HARMONY_ROUTER_PUBLIC_ERRORS 1
ln -sfn "$R" "$B/current.new" && mv -T "$B/current.new" "$B/current"; echo "current -> releases/$rev"
if [ "$DRY" = 1 ]; then echo "dry run: no quiet check, restart or health probe"; exit 0; fi
"$B/relay-quiet.sh" 480 || { echo "relay never went quiet in 8 min: NOT restarting (code and env are switched; restart or rollback-router.sh later)"; exit 2; }
systemctl --user restart "$UNIT"; sleep 3
for _ in 1 2 3 4 5; do
  got=$(curl -s "http://127.0.0.1:$PORT/health" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("revision",""))' 2>/dev/null || true)
  [ "$got" = "$rev" ] && { echo "health reports rev $got: deployed"; exit 0; }; sleep 2
done
echo "health did not report $rev (got '${got:-nothing}'): run rollback-router.sh"; exit 3
