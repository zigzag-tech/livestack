"""Actual ASGI HTTP boundaries without engine execution.

Malformed/authentication/body-limit controls cannot certify engine behavior;
successful native generation and GPU cessation require deployed qualification.
"""
import asyncio

from fastapi import FastAPI
import httpx
import pytest

from livestack_node.decisions.vllm_extraction_endpoint import ExecutionEndpointRuntime, ExtractionEndpointPlugin, private_bytes


def post(body, *, token=True, host='127.0.0.1', configured=True):
    async def run():
        app = FastAPI(); ExtractionEndpointPlugin().attach_router(app)
        if configured:
            # No engine substitute: these boundaries must stop before dispatch.
            app.state.harmony_extraction_execution = ExecutionEndpointRuntime(
                'fixture-control-token-with-32-characters', 'llm_general', None, None)
        transport = httpx.ASGITransport(app=app, client=(host, 10000))
        async with httpx.AsyncClient(transport=transport, base_url='http://fixture') as client:
            headers = {'authorization': 'Bearer fixture-control-token-with-32-characters',
                'x-harmony-execution-id': 'a' * 32} if token else {}
            return await client.post('/harmony/extraction', content=body, headers=headers)
    return asyncio.run(run())


@pytest.mark.parametrize('body', [b'{}', b'not JSON', b'x' * 65537])
def test_authenticated_invalid_body_is_refused_without_engine(body):
    response = post(body)
    assert response.status_code == 400
    assert response.json()['execution_started'] is False
    assert response.json()['cessation_confirmed'] is False


def test_authentication_precedes_body_limit():
    assert post(b'x' * 65537, token=False).status_code == 401


def test_remote_and_unconfigured_callers_are_named_refusals():
    assert post(b'{}', host='192.0.2.1').status_code == 403
    assert post(b'{}', configured=False).status_code == 503


def test_slow_authenticated_body_occupies_only_one_ingress_slot():
    async def run():
        app = FastAPI(); ExtractionEndpointPlugin().attach_router(app)
        runtime = ExecutionEndpointRuntime('fixture-control-token-with-32-characters', 'llm_general', None, None)
        app.state.harmony_extraction_execution = runtime
        entered, finish = asyncio.Event(), asyncio.Event()
        async def body():
            entered.set(); await finish.wait(); yield b'{}'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            headers = {'authorization': 'Bearer fixture-control-token-with-32-characters',
                'x-harmony-execution-id': 'a' * 32}
            pending = asyncio.create_task(client.post('/harmony/extraction', content=body(), headers=headers))
            await entered.wait()
            rejected = await client.post('/harmony/extraction', content=b'{}', headers=headers)
            assert rejected.status_code == 429
            assert rejected.json()['execution_started'] is False
            finish.set()
            assert (await pending).status_code == 400
            assert not runtime.gate.locked()
    asyncio.run(run())


def test_private_configuration_rejects_public_permissions_and_symlinks(tmp_path):
    config = tmp_path / 'config'; config.write_bytes(b'{}'); config.chmod(0o600)
    assert private_bytes(str(config), 4096) == b'{}'
    config.chmod(0o644)
    with pytest.raises(ValueError): private_bytes(str(config), 4096)
    config.chmod(0o600)
    link = tmp_path / 'link'; link.symlink_to(config)
    with pytest.raises(OSError): private_bytes(str(link), 4096)
