#!/bin/bash
# Remove ONLY the harmony-asr target (every other relay setting is left as it is now), restart the relay once when idle.
# `--unit` also stops and disables the router unit.
set -e
B=~/.local/share/harmony-speech-router; CFG=/etc/benchday/relay/regional-relay-zz-tower2-cn.json
$B/relay-quiet.sh 480 || { echo "relay never went quiet in 8 min: NOT restarting (nothing changed in the relay)"; exit 2; }
ts=$(date -u +%Y%m%dT%H%M%SZ); sudo cp -p $CFG $CFG.bak.$ts; echo "backup: $CFG.bak.$ts"
sudo python3 $B/relay_target.py remove
sudo systemctl restart benchday-regional-relay; sleep 3
systemctl is-active benchday-regional-relay
[ "$1" = "--unit" ] && systemctl --user disable --now harmony-speech-router.service
exit 0
