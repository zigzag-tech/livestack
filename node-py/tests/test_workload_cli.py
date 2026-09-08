"""Real CLI processes round-trip source bytes through the authenticated API."""
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread

from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore


def test_cli_bundles_uploads_submits_and_downloads(tmp_path):
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'check.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [Principal('owner', 'a'*32, 'caller', ('check.v1',))])
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path/'client.json'
    config.write_text(json.dumps({'authority': f'http://127.0.0.1:{server.server_port}', 'token': 'a'*32}))
    source = tmp_path/'source'
    source.mkdir()
    (source/'input').write_text('private source')
    inventory = tmp_path/'inventory.json'
    inventory.write_text(json.dumps({'paths': ['input'], 'provenance': {'test': 'cli'}}))
    def cli(*args):
        reply = subprocess.run([sys.executable, '-m', 'livestack_node.workloads.cli', '--config', str(config), *map(str,args)],
            env=dict(os.environ), capture_output=True, text=True, timeout=10)
        assert reply.returncode == 0, reply.stderr
        return json.loads(reply.stdout)
    try:
        bundle = cli('bundle', source, inventory, tmp_path/'source.tar')
        uploaded = cli('upload', tmp_path/'source.tar')
        assert uploaded == bundle
        request = tmp_path/'job.json'
        request.write_text(json.dumps({'version':1,'key':'cli','handler':'check.v1','input_digest':bundle['digest'],
            'need':{'cpu':1},'payload':{}}))
        job = cli('submit', request)
        assert cli('get', job['id'])['spec']['input_digest'] == bundle['digest']
        cli('download', bundle['digest'], tmp_path/'returned.tar')
        assert (tmp_path/'returned.tar').read_bytes() == (tmp_path/'source.tar').read_bytes()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
