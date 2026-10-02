"""Real kernel refusals and private Docker connectivity; no network fake."""
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid

import pytest

from livestack_node.workloads.docker_runtime import prepare, remove_data, runtime_path
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.supervision import SystemdExecutor

IMAGE = 'docker.m.daocloud.io/library/alpine@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d'


@pytest.mark.parametrize('address,reason', [
    (None, 'missing'), ('localhost', 'invalid'), ('127.0.0.1', 'invalid'),
    ('0.0.0.0', 'invalid'), ('224.0.0.1', 'invalid'), ('169.254.1.1', 'invalid'),
    ('::1', 'invalid'), ('203.0.113.77', 'not_local'),
])
def test_native_route_refuses_before_creating_runtime(tmp_path, address, reason, monkeypatch):
    # An inherited source environment is deliberately not a declaration.
    monkeypatch.setenv('HARMONY_NATIVE_HOST_ADDRESS', '100.64.0.24')
    unit = 'harmony-network-refusal-'+uuid.uuid4().hex+'.service'
    with pytest.raises(WorkloadError, match='native_docker_host_address_'+reason):
        prepare(unit, ['/bin/true'], tmp_path, tmp_path,
                native_client=True, native_host_address=address)
    assert not runtime_path(unit).exists()
    assert not (tmp_path/'docker-execution.json').exists()


def test_native_host_gateway_reaches_handler_and_preserves_peer_identity(tmp_path):
    if not all(shutil.which(tool) for tool in ('rootlesskit', 'slirp4netns', 'newuidmap', 'dockerd')):
        pytest.skip('requires installed private rootless Docker prerequisites')
    address = os.environ.get('HARMONY_TEST_NATIVE_HOST_ADDRESS')
    if address is None:
        pytest.skip('requires an explicit operator-declared native test host address')
    executor = SystemdExecutor('native-route-'+uuid.uuid4().hex)
    attempt = uuid.uuid4().hex
    output = tmp_path/'output'
    output.mkdir()
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.settimeout(90)
    endpoint = Path('/run/user')/str(os.getuid())/('hn-'+uuid.uuid4().hex+'.sock')
    peer.bind(str(endpoint))
    peer.listen(1)
    script = tmp_path/'handler.py'
    script.write_text('''import json,os,socket,subprocess,sys,threading
from http.server import HTTPServer,BaseHTTPRequestHandler
from pathlib import Path
image,output,endpoint,address=sys.argv[1:]
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'native-route-positive-control')
 def log_message(self,*args):pass
peer=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);peer.connect(endpoint);peer.sendall(b'host-identity');peer.close()
server=HTTPServer(('0.0.0.0',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
url='http://host.docker.internal:'+str(server.server_port)+'/private-control'
try:
 command=['docker','run','--rm','--add-host=host.docker.internal:host-gateway',image,'sh','-c',
          'grep host.docker.internal /etc/hosts; wget -q -O - "$1"','control',url]
 result=subprocess.run(command,capture_output=True,text=True,timeout=60,check=True)
 assert address in result.stdout,result.stdout
 assert 'native-route-positive-control' in result.stdout,result.stdout
 Path(output,'network-proof.json').write_text(json.dumps({'uid':os.getuid(),'netns':os.readlink('/proc/self/ns/net'),'address':address,'response':result.stdout}))
finally:server.shutdown()
''')
    try:
        executor.start(attempt, [sys.executable, str(script), IMAGE, str(output), str(endpoint), address],
                       tmp_path, output, env=dict(os.environ), cpu=1, memory_bytes=768*1024**2,
                       rootless_docker=True, rootless_native=True, native_host_address=address,
                       max_seconds=120, tasks=512)
        connection, _ = peer.accept()
        with connection:
            pid, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            assert connection.recv(64) == b'host-identity'
            assert uid == os.getuid()
            assert os.readlink(f'/proc/{pid}/ns/user') == os.readlink('/proc/self/ns/user')
        deadline = time.monotonic()+100
        result = None
        while time.monotonic() < deadline:
            result = executor.exit_result(output)
            if result is not None:
                break
            time.sleep(.1)
        assert result is not None and result['exit_code'] == 0, result
        proof = json.loads((output/'network-proof.json').read_text())
        assert proof['netns'] == os.readlink('/proc/self/ns/net')
        assert proof['uid'] == os.getuid()
    finally:
        peer.close()
        endpoint.unlink(missing_ok=True)
        executor.stop(attempt)
        remove_data(tmp_path)
