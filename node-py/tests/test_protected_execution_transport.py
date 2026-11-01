"""Real HTTP receipt boundaries, not actual engine/GPU qualification."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import httpx
import pytest

from livestack_node.decisions.contract import ContractError
from livestack_node.decisions.execution_uncertain import ExecutionUnconfirmed
from livestack_node.decisions.protected_execution_transport import ProtectedExecutionTransport


@pytest.fixture
def receipt_http():
    state = {'fault': None, 'requests': []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['content-length']))
            nonce = self.headers.get('x-harmony-execution-id')
            deadline = self.headers.get('x-harmony-execution-deadline-ms')
            state['requests'].append({'nonce': nonce, 'deadline': deadline})
            identity = 'chatcmpl-' + nonce
            status = 200
            value = {'id': identity, 'choices': [], 'harmony_execution':
                     {'request_id': identity, 'completion_confirmed': True}}
            fault = state['fault']
            if fault in ('stopped', 'uncertain', 'not_started', 'wrong_error_id'):
                status = 504
                value = {'request_id': identity, 'cause': 'deadline_exceeded',
                         'execution_started': fault != 'not_started',
                         'cessation_confirmed': fault != 'uncertain'}
                if fault == 'wrong_error_id':value['request_id'] = 'chatcmpl-other'
            if fault == 'wrong_completion_id':value['harmony_execution']['request_id'] = 'chatcmpl-other'
            raw = b'invalid JSON' if fault == 'invalid_json' else json.dumps(value).encode()
            if fault == 'overflow':raw = b'x' * 65537
            self.send_response(status);self.end_headers();self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    yield f'http://127.0.0.1:{server.server_port}/harmony/extraction', state
    server.shutdown();server.server_close();thread.join()


def generate(endpoint):
    async def run():
        async with httpx.AsyncClient() as client:
            return await ProtectedExecutionTransport(client=client, endpoint=endpoint).generate(
                {}, deadline_at_ms=int(time.time()*1000)+8000)
    return asyncio.run(run())


def test_correlated_completion_and_absolute_deadline_are_transported(receipt_http):
    endpoint, state = receipt_http
    value = generate(endpoint)
    assert value['id'] == 'chatcmpl-' + state['requests'][0]['nonce']
    assert len(state['requests'][0]['nonce']) == 32
    assert int(state['requests'][0]['deadline']) > int(time.time()*1000)


@pytest.mark.parametrize('fault', ['stopped', 'not_started'])
def test_only_correlated_non_execution_or_stop_releases_error_path(receipt_http, fault):
    endpoint, state = receipt_http;state['fault'] = fault
    with pytest.raises(ContractError) as caught:generate(endpoint)
    assert not isinstance(caught.value, ExecutionUnconfirmed)
    assert caught.value.cause == 'deadline_exceeded'


@pytest.mark.parametrize('fault', ['uncertain', 'wrong_error_id', 'wrong_completion_id', 'invalid_json', 'overflow'])
def test_missing_or_uncorrelated_receipt_retains_reservations(receipt_http, fault):
    endpoint, state = receipt_http;state['fault'] = fault
    with pytest.raises(ExecutionUnconfirmed):generate(endpoint)


@pytest.mark.parametrize('endpoint', ['https://127.0.0.1/harmony/extraction',
    'http://example.com/harmony/extraction', 'http://127.0.0.1/v1/chat/completions',
    'http://credential@127.0.0.1/harmony/extraction'])
def test_transport_refuses_remote_or_credential_bearing_urls(endpoint):
    with pytest.raises(ContractError):ProtectedExecutionTransport(client=None, endpoint=endpoint)
