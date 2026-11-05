#!/bin/bash
# exit 0 when the relay holds NO outbound connection (to an engine, a daemon terminal tunnel, the
# vendor): no relayed speech/terminal session is in flight, so a relay restart drops no one.
# Inbound connections on :8767 (nginx, hub control) reconnect on their own and are ignored.
# Polls every 2 s for up to $1 seconds (default 480, always < 9 min); exit 1 if it never goes quiet.
limit=${1:-480}; end=$((SECONDS+limit))
while [ $SECONDS -lt $end ]; do
  rp=$(systemctl show -p MainPID --value benchday-regional-relay)
  n=$(sudo ss -tnp state established 2>/dev/null | awk -v p="pid=$rp," "index(\$0,p) && \$3 !~ /:8767\$/ {n++} END {print n+0}")
  [ "$n" = "0" ] && exit 0
  sleep 2
done
exit 1
