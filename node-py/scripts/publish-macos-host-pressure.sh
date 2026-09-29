#!/bin/bash
# Publish this Mac's memory pressure for a Harmony worker running in a VM on it.
#
# A worker in a Lima/VZ guest reads the guest's /proc/meminfo, which looks healthy
# while the macOS host swaps. Writes {"ts", "available_memory_bytes"} atomically to
# $OUT (default ~/.local/state/host-pressure.json, visible read-only inside a Lima
# guest that mounts the host home). The worker consumes it via its
# `host_pressure` config: {"path": "/Users/<user>/.local/state/host-pressure.json"}.
#
# available_memory_bytes = reclaimable pages (free+inactive+speculative+purgeable),
# or 0 while the host is actively swapping (> SWAP_PAGES_PER_SEC swapins+swapouts,
# measured since the previous run). Swap USED is deliberately not the signal: macOS
# never shrinks it, so it would keep a healthy host unschedulable for days.
# Failure to publish leaves the file to go stale, which the worker reports as 0.
#
# Env: SWAP_PAGES_PER_SEC (default 500 = ~8 MB/s at 16 KB pages), OUT.
# Install: publish-macos-host-pressure.sh --install   (LaunchAgent, every 15 s)
set -euo pipefail

OUT="${OUT:-$HOME/.local/state/host-pressure.json}"
SWAP_PAGES_PER_SEC="${SWAP_PAGES_PER_SEC:-500}"
LABEL=io.zigzag.harmony-host-pressure

if [[ "${1:-}" == "--install" ]]; then
  bin="$HOME/.local/libexec/harmony-host-pressure"
  mkdir -p "$(dirname "$bin")" "$(dirname "$OUT")" "$HOME/Library/LaunchAgents"
  cp "$0" "$bin" && chmod +x "$bin"
  cat >"$HOME/Library/LaunchAgents/$LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$bin</string></array>
  <key>StartInterval</key><integer>15</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$HOME/.local/state/harmony-host-pressure.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$LABEL.plist"
  echo "installed $LABEL -> $OUT"
  exit 0
fi

mkdir -p "$(dirname "$OUT")"
vm="$(vm_stat)"
now="$(date +%s)"
read -r page_size <<<"$(sed -n 's/.*page size of \([0-9]*\) bytes.*/\1/p' <<<"$vm")"
pages() { awk -v k="$1" -F: '$1==k {gsub(/[ .]/,"",$2); print $2}' <<<"$vm"; }
reclaimable=$(( $(pages "Pages free") + $(pages "Pages inactive") + $(pages "Pages speculative") + $(pages "Pages purgeable") ))
swaps=$(( $(pages "Swapins") + $(pages "Swapouts") ))
available=$(( reclaimable * page_size ))

# Rate since the previous run; the first run has no baseline and reports the reclaimable figure.
prev="$OUT.swap"
if [[ -f "$prev" ]]; then
  read -r prev_ts prev_swaps <"$prev" || true
  elapsed=$(( now - ${prev_ts:-now} ))
  if (( elapsed > 0 )) && (( (swaps - prev_swaps) / elapsed > SWAP_PAGES_PER_SEC )); then
    available=0
  fi
fi
echo "$now $swaps" >"$prev"

tmp="$(mktemp "$OUT.XXXXXX")"
printf '{"ts": %s, "available_memory_bytes": %s}\n' "$now" "$available" >"$tmp"
chmod 644 "$tmp"
mv "$tmp" "$OUT"
