#!/bin/bash
# Probe: is the unit cgroup readable after the unit enters failed (OOM kill)?
# Usage: cgprobe.sh <mode>  mode=direct (main process allocates) | child (wrapper spawns allocating child)
mode=$1; U=lsprobe-oom-$$
if [ "$mode" = direct ]; then
  CMD='b=bytearray(256*1024*1024)
for i in range(0,len(b),4096): b[i]=1
import time; time.sleep(30)'
else
  CMD='import subprocess,sys,time
subprocess.Popen([sys.executable,"-c","b=bytearray(256*1024*1024)\nfor i in range(0,len(b),4096): b[i]=1\nimport time; time.sleep(30)"])
time.sleep(30)'
fi
systemd-run --user --quiet --unit=$U -p Type=exec -p OOMPolicy=kill -p MemoryMax=64M -p MemorySwapMax=0 \
  -p KillMode=control-group -p PrivateTmp=yes python3 -c "$CMD" || { echo launch failed; exit 2; }
CG=""
for i in $(seq 1 100); do
  st=$(systemctl --user show $U -p ActiveState --value)
  [ -z "$CG" -o "$CG" = / ] && CG=$(systemctl --user show $U -p ControlGroup --value)
  if [ "$st" = failed ]; then
    echo "t=$((i*100))ms state=failed cg=$CG"
    for s in 0 0.5 1 3; do sleep $s
      echo "+${s}s: dir=$([ -d /sys/fs/cgroup$CG ] && echo present || echo GONE) events=$(tr '\n' ' ' </sys/fs/cgroup$CG/memory.events 2>&1) peak=$(cat /sys/fs/cgroup$CG/memory.peak 2>&1)"
    done
    systemctl --user show $U -p Result,ExecMainStatus,MemoryPeak,CPUUsageNSec,TasksCurrent,IOReadBytes,OOMKills,ControlGroup,ActiveState,SubState
    systemctl --user reset-failed $U; exit 0
  fi
  sleep 0.1
done
echo "never failed: $st"; systemctl --user stop $U
