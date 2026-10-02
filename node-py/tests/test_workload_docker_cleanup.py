"""Real UID namespace and filesystem cleanup; requires Linux rootless tools."""
import os
import subprocess
import tempfile

import pytest

from livestack_node.workloads.docker_runtime import remove_data
from livestack_node.workloads.model import WorkloadError


def test_subordinate_uid_cleanup_and_bounded_permission_failure(tmp_path, monkeypatch):
    assert os.getuid()!=0, 'cleanup control requires the host non-root principal'
    positive=tmp_path/'positive';positive.mkdir()
    data=positive/'docker-data';data.mkdir()
    directory=data/'subordinate';directory.mkdir()
    (directory/'file').write_bytes(b'owned control')
    with tempfile.TemporaryDirectory(prefix='hcontrol-',dir='/run/user/'+str(os.getuid())) as state:
        subprocess.run(['/usr/bin/rootlesskit','--state-dir='+state,'/usr/bin/chown','-R','1:1',str(directory)],
                       check=True,timeout=10)
    # Actual nested worker paths can exceed Unix socket pathname capacity.
    long_temp=tmp_path/('nested-attempt-'*10);long_temp.mkdir()
    monkeypatch.setenv('TMPDIR',str(long_temp))
    remove_data(positive)
    assert not data.exists()

    negative=tmp_path/'negative';negative.mkdir()
    data=negative/'docker-data'
    subprocess.run(['sudo','-n','mkdir','-m','700',str(data)],check=True,timeout=5)
    try:
        subprocess.run(['sudo','-n','touch',str(data/'host-root-owned')],check=True,timeout=5)
        with pytest.raises(WorkloadError) as refusal:
            remove_data(negative)
        message=str(refusal.value)
        assert 'capacity remains reserved' in message
        assert 'exit=1' in message and 'Permission denied' in message
        assert len(message.encode())<=4096
        assert data.exists(), 'refused cleanup must not masquerade as removed data'
    finally:
        # Exact owned data, after the finite cleanup command has terminated.
        subprocess.run(['sudo','-n','rm','-rf','--',str(data)],check=True,timeout=5)
