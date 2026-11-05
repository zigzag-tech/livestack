#!/bin/bash
# Add the relay target harmony-asr (-> router 127.0.0.1:8189) ADDITIVELY and restart the relay once, when idle.
# Relay target ONLY. Router code is deployed/rolled back by deploy-router.sh / rollback-router.sh.
set -e
B=~/.local/share/harmony-speech-router; CFG=/etc/benchday/relay/regional-relay-zz-tower2-cn.json
systemctl --user enable --now harmony-speech-router.service
$B/relay-quiet.sh 480 || { echo "relay never went quiet in 8 min: NOT restarting (nothing changed in the relay)"; exit 2; }
ts=$(date -u +%Y%m%dT%H%M%SZ); sudo cp -p $CFG $CFG.bak.$ts; echo "backup: $CFG.bak.$ts"
sudo python3 $B/relay_target.py add
sudo systemctl restart benchday-regional-relay; sleep 3
systemctl is-active benchday-regional-relay
