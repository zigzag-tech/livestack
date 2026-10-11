"""Read-only physical CUDA identity without importing/initializing Torch."""
import hashlib
import os
from pathlib import Path
import re
import socket
import stat
import subprocess


def qualification_cuda_device(index, expected_device, executable_sha256):
    if index not in ('0','1') or not re.fullmatch('[a-f0-9]{64}',executable_sha256 or ''):
        raise ValueError('qualification_explicit_physical_cuda_toolchain_required')
    path=Path('/usr/bin/nvidia-smi').resolve(strict=True);info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or hashlib.sha256(path.read_bytes()).hexdigest()!=executable_sha256:
        raise PermissionError('qualification_nvml_executable_changed')
    result=subprocess.run([str(path),'--query-gpu=index,uuid','--format=csv,noheader,nounits'],env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},capture_output=True,text=True,check=True,timeout=20)
    if len(result.stdout)>4096:raise ValueError('qualification_cuda_inventory_bound')
    rows=[line.strip().split(', ') for line in result.stdout.splitlines() if line.strip()]
    selected=[row for row in rows if len(row)==2 and row[0]==index]
    if len(rows)>8 or len(selected)!=1 or not re.fullmatch(r'GPU-[a-fA-F0-9-]{36}',selected[0][1]):
        raise PermissionError('qualification_actual_physical_cuda_uuid_required')
    actual=socket.gethostname()+'/'+hashlib.sha256(selected[0][1].encode()).hexdigest()[:8]
    if actual!=expected_device:raise PermissionError('qualification_actual_device_binding_changed')
    return actual
