"""Launch the real authority CLI and verify configured storage admission."""
import json
import re
import subprocess
import sys
import time
import urllib.error

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.transfer import InputTransfer


def test_service_applies_operator_blob_quota(tmp_path):
    config = tmp_path/'config.json'
    state = tmp_path/'state'
    config.write_text(json.dumps(dict(state_dir=str(state),port=0,handlers=['test.v1'],
        blob_limits={'max_bytes':8},principals=[dict(id='owner',token='a'*32,role='caller',handlers=['test.v1'])])))
    process = subprocess.Popen([sys.executable,'-m','livestack_node.workloads.service','--config',str(config)])
    try:
        deadline = time.monotonic()+15
        log = state/'authority.log'
        match = None
        while not match:
            if log.exists():
                match = re.search(r"started on \('127.0.0.1', (\d+)\)", log.read_text())
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.05)
        client = WorkloadClient('http://127.0.0.1:'+match[1], 'a'*32)
        first, second = tmp_path/'first', tmp_path/'second'
        first.write_bytes(b'1234567')
        second.write_bytes(b'89')
        InputTransfer(client).put(first)
        with pytest.raises(urllib.error.HTTPError) as error:
            InputTransfer(client).put(second)
        assert error.value.code == 429
    finally:
        process.terminate()
        process.wait(timeout=10)
