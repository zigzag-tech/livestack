"""Real rootless Docker and cgroups; no mock can prove container ownership."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from livestack_node.workloads.docker_runtime import remove_data, runtime_path
from livestack_node.workloads.supervision import SystemdExecutor

IMAGE = 'docker.m.daocloud.io/library/alpine@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d'


def until(predicate, seconds=90):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError('Docker integration condition did not become true')


@pytest.mark.parametrize('outcome', ['success', 'failure', 'lease-expired'])
def test_private_docker_inherits_job_caps_and_stops_with_lease(tmp_path, outcome):
    if not all(shutil.which(tool) for tool in ('rootlesskit', 'slirp4netns', 'newuidmap', 'dockerd')):
        pytest.skip('requires installed rootless Docker prerequisites')
    executor = SystemdExecutor('docker-integration-'+uuid.uuid4().hex)
    attempt = uuid.uuid4().hex
    output = tmp_path/'output'
    output.mkdir()
    lease = tmp_path/'lease'
    lease.write_text(str(time.monotonic()+150))
    script = tmp_path/'handler.py'
    script.write_text('''import json,os,subprocess,time
from pathlib import Path
image,output,outcome = __import__('sys').argv[1:]
assert not Path('/var/run/docker.sock').exists(), 'host Docker socket leaked into namespace'
subprocess.run(['docker','run','-d','--name=proof',image,'sleep','120'],check=True)
pid=int(subprocess.check_output(['docker','inspect','--format','{{.State.Pid}}','proof'],text=True))
group=Path(f'/proc/{pid}/cgroup').read_text().strip().split('::',1)[1]
Path(output,'proof.json').write_text(json.dumps({'pid':pid,'group':group}))
if outcome=='success':
    build=Path(output).parent/'build-proof'
    build.mkdir()
    marker='harmony-build-'+__import__('uuid').uuid4().hex
    (build/'Dockerfile').write_text('FROM '+image+'\\nRUN echo '+marker+'; sleep 6; echo build-finished\\n')
    process=subprocess.Popen(['docker','build','--progress=plain',str(build)])
    deadline=time.monotonic()+60
    observed=[]
    while time.monotonic()<deadline and process.poll() is None:
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit(): continue
            try:
                if marker.encode() in (entry/'cmdline').read_bytes():
                    observed.append((entry/'cgroup').read_text().strip().split('::',1)[1])
            except (OSError,ValueError): pass
        if observed: break
        time.sleep(.1)
    if process.wait(timeout=60): raise RuntimeError('private Docker build failed')
    Path(output,'build-proof.json').write_text(json.dumps(observed))

if outcome=='lease-expired': time.sleep(120)
raise SystemExit(7 if outcome=='failure' else 0)
''')
    try:
        executor.start(attempt, [sys.executable, str(script), IMAGE, str(output), outcome],
            tmp_path, output, env=dict(os.environ), cpu=1, memory_bytes=512*1024**2,
            rootless_docker=True, lease_file=lease, max_seconds=180)
        proof = until(lambda: json.loads((output/'proof.json').read_text()) if (output/'proof.json').exists() else None)
        group = executor.inspect(attempt)['ControlGroup']
        assert proof['group'].startswith(group+'/'), proof
        cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
        assert (cgroup/'memory.max').read_text().strip() == str(512*1024**2)
        assert (cgroup/'cpu.max').read_text().strip() == '100000 100000'
        if outcome == 'lease-expired':
            lease.write_text('0')
            until(lambda: not cgroup.exists() or 'populated 0' in (cgroup/'cgroup.events').read_text(), 15)
            assert executor.exit_result(output) is None
        else:
            result = until(lambda: executor.exit_result(output), 90)
            assert result['exit_code'] == (7 if outcome == 'failure' else 0)
            if outcome == 'success':
                groups = json.loads((output/'build-proof.json').read_text())
                assert groups and all(path.startswith(group+'/') for path in groups), groups
        executor.stop(attempt)
        assert not Path('/proc', str(proof['pid'])).exists()
        assert not runtime_path(executor.unit(attempt)).exists()
    finally:
        executor.stop(attempt)
        remove_data(tmp_path)
