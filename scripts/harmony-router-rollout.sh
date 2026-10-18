#!/usr/bin/env bash
# Install / update / inspect the Harmony router on requester machines.
#
#   scripts/harmony-router-rollout.sh [host…] [--status] [--rev <sha>]
#
# With no host: every machine in HOSTS below. A router is a facade with zero units
# (node-py/examples/harmony-llm/router.py) bound to 127.0.0.1:8188; GPU hosts get
# none. It is pinned to the revision the SERVING nodes report on their /health,
# not to whatever repo-sync last pulled: a router and its targets must agree on
# the routed-request contract, so a GPU redeploy is followed by a rollout.
#
# Idempotent: a second run against an up-to-date host reports "no changes" and
# restarts nothing. After installing, the router must answer a /harmony/route
# dry run that NAMES a target, or the host is reported failed.
#
#   HARMONY_ROUTER_PIP_INDEX   pip index for hosts that cannot reach pypi.org
#                              (cn hosts default to a CN mirror)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
UNIT_DIR="$REPO/node-py/examples/harmony-llm/systemd/router"

# name|ssh target ("local" = this machine)|region|fleet broker for that region|fleetd
# fleetd=1: this host also runs the region's fleet broker (observe-only), `cn` on
# zz-tower2. The `na` broker is livestack-fleetd on xc-tower-ubuntu, not ours.
HOSTS=(
  "lappy-bellinzona|local|na|http://100.64.0.18:8801|0"
  "xc-mac-studio|ubuntu@100.64.0.2|na|http://100.64.0.18:8801|0"
  "zz-tower0|ubuntu@100.64.0.3|cn|http://100.64.0.12:8801|0"
  "zz-tower2|ubuntu@100.64.0.12|cn|http://100.64.0.12:8801|1"
  # WSL2 in mirrored-network mode shares loopback with Windows, so this one router
  # also answers 127.0.0.1:8188 for Windows-native callers. `#2222` is the ssh port.
  "xc-win-1|ubuntu@100.64.0.1#2222|cn|http://100.64.0.12:8801|0"
  # The public hub box (Tokyo): neither na nor cn, so no region (vantage = the fleet
  # broker's own probe). Installing the router is additive; pointing the hub at it
  # is a hub env change and a hub restart, which goes through the fenced deploy.
  "public-tokyo|ubuntu@100.64.0.17||http://100.64.0.18:8801|0"
)
# What a regional fleet broker watches: every llm node, and the cn hosts.
FLEETD_PEERS="http://100.64.0.18:8188/livestack,http://100.64.0.27:8188/livestack,http://100.64.0.3:8100/livestack,http://100.64.0.3:8766/livestack,http://100.64.0.3:8844/livestack"
FLEETD_LINK_PEERS="zz-tower0=http://100.64.0.3:8799,xc-tower-ubuntu=http://100.64.0.18:8799,zz-whitby=http://100.64.0.27:8799"
# The serving nodes whose /health says which revision routers must run.
SERVING=(http://100.64.0.18:8188 http://100.64.0.27:8188)
# Where a host that cannot reach GitHub fetches the pinned revision from.
PEER_REPO="ubuntu@100.64.0.18:livestack"
ORIGIN_URL="$(git -C "$REPO" remote get-url origin 2>/dev/null || echo git@github.com:zigzag-tech/livestack.git)"

STATUS=0; REV=""; WANT=()
while [ $# -gt 0 ]; do
  case "$1" in
    --status) STATUS=1 ;;
    --rev) REV="$2"; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) WANT+=("$1") ;;
  esac
  shift
done

serving_rev() {
  local revs=() u r
  for u in "${SERVING[@]}"; do
    r="$(curl -fsS --max-time 8 "$u/health" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("revision",""))' 2>/dev/null || true)"
    [ -n "$r" ] && [ "$r" != unknown ] || { echo "serving node $u reports no revision (redeploy it first)" >&2; return 1; }
    revs+=("$r")
  done
  [ "$(printf '%s\n' "${revs[@]}" | sort -u | wc -l)" = 1 ] \
    || { echo "serving nodes run different revisions: ${revs[*]} — pass --rev to choose" >&2; return 1; }
  echo "${revs[0]}"
}

# The part that runs ON the host. Reads its parameters from the environment.
remote_install() { cat <<'REMOTE'
set -euo pipefail
changed=()
SHARE="$HOME/.local/share/harmony-router"; SRC="$SHARE/src"; VENV="$SHARE/venv"
CONF="$HOME/.config/harmony-router"; mkdir -p "$SHARE" "$CONF"
OS="$(uname -s)"

# 1. source at the pinned revision (a worktree of ~/livestack when there is one).
if [ ! -e "$SRC/.git" ]; then
  if [ -d "$HOME/livestack/.git" ]; then
    git -C "$HOME/livestack" fetch -q origin 2>/dev/null || true
    git -C "$HOME/livestack" worktree add -q --detach "$SRC" HEAD
  else
    git clone -q --no-checkout "$ORIGIN_URL" "$SRC" || git clone -q --no-checkout "$PEER_REPO" "$SRC"
  fi
  changed+=(source)
fi
if [ "$(git -C "$SRC" rev-parse HEAD)" != "$REV" ]; then
  git -C "$SRC" cat-file -e "$REV^{commit}" 2>/dev/null \
    || git -C "$SRC" fetch -q origin "$REV" 2>/dev/null \
    || git -C "$SRC" fetch -q "$PEER_REPO" "$REV"
  git -C "$SRC" checkout -q --detach "$REV"
  changed+=("rev=$REV")
fi

# A fresh --no-checkout clone already has HEAD at the pinned revision and an
# empty tree: populate it.
if [ ! -f "$SRC/node-py/examples/harmony-llm/router.py" ]; then
  git -C "$SRC" checkout -q -f --detach "$REV"; changed+=(checkout)
fi

# 2. venv with the router's few dependencies (marker = the dependency set).
DEPS="fastapi uvicorn httpx pydantic cryptography"
if [ ! -x "$VENV/bin/python" ] || [ "$(cat "$SHARE/.deps" 2>/dev/null)" != "$DEPS" ]; then
  idx=(); [ -n "${PIP_INDEX:-}" ] && idx=(--index-url "$PIP_INDEX")
  if command -v uv >/dev/null; then
    uv venv -q "$VENV" && VIRTUAL_ENV="$VENV" uv pip install -q "${idx[@]}" $DEPS
  else
    python3 -m venv "$VENV" && "$VENV/bin/pip" install -q "${idx[@]}" $DEPS
  fi
  echo "$DEPS" > "$SHARE/.deps"; changed+=(venv)
fi

# 3. env file (no fleet token, ever).
want_env="HARMONY_LLM_FLEET_URL=$FLEET_URL
LIVESTACK_NODE_REGION=$REGION
HARMONY_ROUTER_HOST=127.0.0.1
HARMONY_ROUTER_PORT=8188
HARMONY_ROUTER_ID=router-$NAME
HARMONY_DEMAND_LOG_AGE_DAYS=21"
if [ "$(cat "$CONF/env" 2>/dev/null)" != "$want_env" ]; then
  printf '%s\n' "$want_env" > "$CONF/env"; changed+=(env)
fi

# 4. the service.
restart=0
if [ "$OS" = Darwin ]; then
  PLIST="$HOME/Library/LaunchAgents/io.zztech.harmony-router.plist"; mkdir -p "$(dirname "$PLIST")" "$HOME/Library/Logs"
  envxml="$(while IFS== read -r k v; do printf '    <key>%s</key><string>%s</string>\n' "$k" "$v"; done <<<"$want_env")"
  rendered="$(ENVXML="$envxml" T="$PLIST_TEMPLATE" python3 -c 'import os; print(os.environ["T"].replace("__HOME__", os.environ["HOME"]).replace("__ENV__", os.environ["ENVXML"]), end="")')"
  if [ "$(cat "$PLIST" 2>/dev/null)" != "$rendered" ]; then
    printf '%s\n' "$rendered" > "$PLIST"; plutil -lint "$PLIST" >/dev/null; changed+=(plist); restart=1
  fi
  launchctl print "gui/$(id -u)/io.zztech.harmony-router" >/dev/null 2>&1 || { launchctl bootstrap "gui/$(id -u)" "$PLIST"; changed+=(started); }
  if [ "$restart" = 1 ] || [[ " ${changed[*]} " == *" rev="* ]] || [[ " ${changed[*]} " == *" env "* ]]; then
    launchctl kickstart -k "gui/$(id -u)/io.zztech.harmony-router"
  fi
else
  UD="$HOME/.config/systemd/user"; mkdir -p "$UD"
  if [ "$(cat "$UD/harmony-router.service" 2>/dev/null)" != "$UNIT" ]; then
    printf '%s\n' "$UNIT" > "$UD/harmony-router.service"; systemctl --user daemon-reload; changed+=(unit); restart=1
  fi
  loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes || { sudo -n loginctl enable-linger "$USER" 2>/dev/null || loginctl enable-linger "$USER"; changed+=(linger); }
  systemctl --user is-enabled harmony-router >/dev/null 2>&1 || { systemctl --user enable harmony-router >/dev/null 2>&1; changed+=(enabled); }
  if ! systemctl --user is-active harmony-router >/dev/null; then
    systemctl --user start harmony-router; changed+=(started)
  elif [ "$restart" = 1 ] || [[ " ${changed[*]} " == *" rev="* ]] || [[ " ${changed[*]} " == *" env "* ]]; then
    systemctl --user restart harmony-router
  fi
fi

# 4b. the region's fleet broker, where this host is the one (Linux only).
if [ "${FLEETD:-0}" = 1 ] && [ "$OS" != Darwin ]; then
  want_fenv="LIVESTACK_HOST_ID=$NAME
LIVESTACK_PEERS=$FLEETD_PEERS
LIVESTACK_LINK_PEERS=$FLEETD_LINK_PEERS"
  frestart=0
  if [ "$(cat "$CONF/fleetd.env" 2>/dev/null)" != "$want_fenv" ]; then
    printf '%s\n' "$want_fenv" > "$CONF/fleetd.env"; changed+=(fleetd-env); frestart=1
  fi
  if [ "$(cat "$UD/harmony-fleetd.service" 2>/dev/null)" != "$FLEETD_UNIT" ]; then
    printf '%s\n' "$FLEETD_UNIT" > "$UD/harmony-fleetd.service"; systemctl --user daemon-reload; changed+=(fleetd-unit); frestart=1
  fi
  systemctl --user is-enabled harmony-fleetd >/dev/null 2>&1 || { systemctl --user enable harmony-fleetd >/dev/null 2>&1; changed+=(fleetd-enabled); }
  if ! systemctl --user is-active harmony-fleetd >/dev/null; then systemctl --user start harmony-fleetd; changed+=(fleetd-started)
  elif [ "$frestart" = 1 ] || [[ " ${changed[*]} " == *" rev="* ]]; then systemctl --user restart harmony-fleetd; fi
  for _ in $(seq 1 30); do curl -fsS --max-time 3 http://127.0.0.1:8801/fleet >/dev/null 2>&1 && break; sleep 1; done
  curl -fsS --max-time 5 http://127.0.0.1:8801/fleet | python3 -c '
import json,sys
v=json.load(sys.stdin)
rows=[(h,n.get("peer"),n.get("region"),n.get("state")) for h,row in v["hosts"].items() for n in row.get("nodes",[])]
print("fleetd: %d node(s): %s" % (len(rows), "; ".join("%s %s %s" % (h,r,s) for h,_p,r,s in rows)))' \
    || { echo "FAILED: regional fleet broker not answering on :8801" >&2; exit 1; }
fi

# 5. it must come up at the pinned revision and name a target.
# (macOS scans a freshly built venv on its first launch; 60+ s is not unusual)
for _ in $(seq 1 120); do
  h="$(curl -fsS --max-time 3 http://127.0.0.1:8188/health 2>/dev/null || true)"
  [[ "$h" == *"\"revision\":\"$REV\""* ]] && break
  sleep 1
done
[[ "$h" == *"\"revision\":\"$REV\""* ]] || { echo "FAILED: router not healthy at $REV: $h" >&2; exit 1; }
echo "${changed[*]:-no changes}" > "$SHARE/.last-run"
echo "CHANGED: ${changed[*]:-no changes}"
REMOTE
}

dry_run() { cat <<'REMOTE'
# A freshly started regional fleet broker needs a few seconds to learn its nodes.
for _ in 1 2 3 4 5 6 7 8; do
  r="$(curl -fsS --max-time 15 -X POST http://127.0.0.1:8188/harmony/route -H 'content-type: application/json' \
    -d '{"model":"require:class=llm","messages":[{"role":"user","content":"x"}]}')" || { echo "dry run: router unreachable"; exit 1; }
  printf '%s' "$r" | grep -q '"chosen":"http' && break
  sleep 3
done
printf '%s' "$r" | python3 -c '
import json,sys
d=json.load(sys.stdin); f=d["fleet"]
print("dry run: chosen=%s expect=%s vantage=%s decision=%s" % (f.get("chosen"), f.get("expect"), f.get("vantage"), f.get("decision")))
sys.exit(0 if f.get("chosen") else 2)'
REMOTE
}

status_script() { cat <<'REMOTE'
h="$(curl -fsS --max-time 3 http://127.0.0.1:8188/health 2>/dev/null)" || { echo "health: router not running"; exit 1; }
printf '%s' "$h" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("health: role=%s revision=%s region=%s fleet=%s contract=%s" % (d.get("role"),d.get("revision","")[:12],d.get("region"),d.get("fleet_url"),d.get("contract")))'
REMOTE
}

run_on() {  # run_on <ssh-target|local> <script-text> [VAR=value…]
  local target="$1" script="$2"; shift 2
  local envs=("$@")
  if [ "$target" = local ]; then env "${envs[@]}" bash -c "$script"
  else
    local port=22; [[ "$target" == *"#"* ]] && { port="${target#*#}"; target="${target%%#*}"; }
    ssh -p "$port" -o ConnectTimeout=10 -o BatchMode=yes "$target" "$(printf '%q ' env "${envs[@]}") bash -s" <<<"$script"
  fi
}

rc=0
[ "$STATUS" = 1 ] || { [ -n "$REV" ] || REV="$(serving_rev)"; echo "pinned revision: $REV"; }
for row in "${HOSTS[@]}"; do
  IFS='|' read -r name target region fleet fleetd <<<"$row"
  if [ ${#WANT[@]} -gt 0 ]; then
    hit=0; for w in "${WANT[@]}"; do [ "$w" = "$name" ] && hit=1; done; [ $hit = 1 ] || continue
  fi
  echo "== $name ($region, fleet $fleet)"
  if [ "$STATUS" = 0 ]; then
    pip_index="${HARMONY_ROUTER_PIP_INDEX:-}"
    [ -z "$pip_index" ] && [ "$region" = cn ] && pip_index="https://pypi.tuna.tsinghua.edu.cn/simple"
    run_on "$target" "$(remote_install)" \
      "REV=$REV" "NAME=$name" "REGION=$region" "FLEET_URL=$fleet" "PIP_INDEX=$pip_index" \
      "ORIGIN_URL=$ORIGIN_URL" "PEER_REPO=$PEER_REPO" \
      "FLEETD=$fleetd" "FLEETD_PEERS=$FLEETD_PEERS" "FLEETD_LINK_PEERS=$FLEETD_LINK_PEERS" \
      "FLEETD_UNIT=$(cat "$UNIT_DIR/harmony-fleetd.service")" \
      "UNIT=$(cat "$UNIT_DIR/harmony-router.service")" \
      "PLIST_TEMPLATE=$(cat "$UNIT_DIR/io.zztech.harmony-router.plist")" || { rc=1; continue; }
  fi
  run_on "$target" "$(status_script)" || { rc=1; continue; }
  run_on "$target" "$(dry_run)" || rc=1
done
exit $rc
