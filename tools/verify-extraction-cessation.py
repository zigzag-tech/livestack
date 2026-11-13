#!/usr/bin/env python3
"""Three admitted real-runtime controls; never a model-quality qualification.

Run after activation with the runtime's private configuration. No install,
restart, model selection override or credential provisioning occurs here.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'node-py'))
import httpx
from livestack_node.decisions.contract import CAUSE_HTTP
from livestack_node.decisions.extraction_contract import FLASH_NEXT_PROFILE_ID, validate_extraction
from livestack_node.decisions.fleet_extraction_admission import FleetExtractionAdmission
from livestack_node.decisions.nominal_27b_adapter import QualifiedEngine, _INSTRUCTIONS
from livestack_node.decisions.pane_ask_runtime_install import validate_registry_profile
from livestack_node.decisions.protected_execution_transport import ProtectedExecutionTransport
from livestack_node.decisions.vllm_extraction_endpoint import private_bytes

MAX_BYTES = 65536
QUESTION = 'Should we ship the patch? Yes or no.'


async def control(name, *, client, fleet, engine, owner, budget_ms, receipts):
    nonce = secrets.token_hex(16)
    identity = 'chatcmpl-' + nonce
    # Long evidence makes the deadline control exercise native prefill/STOP,
    # rather than merely expiring an empty request before dispatch.
    source = (('Background context. ' * 1800) if name == 'deadline' else '') + QUESTION
    body = {'model': engine.unit, 'stream': False, 'temperature': 0,
            'max_tokens': 1024, 'chat_template_kwargs': {'enable_thinking': False},
            'messages': [{'role': 'system', 'content': _INSTRUCTIONS},
                         {'role': 'user', 'content': json.dumps({'current_agent_message': source})}]}
    if len(json.dumps(body).encode()) > MAX_BYTES:
        raise RuntimeError('control request exceeds 64 KiB')
    lease_deadline = int(time.time() * 1000) + 8000
    grant = await fleet.admit(owner=owner, realm='benchday', profile_id=engine.profile_id,
                             request_id=identity, deadline_at_ms=lease_deadline)
    summary = {'control': name, 'request_id': identity, 'lease_id': grant.lease_id,
               'lease_released': False, 'status': None}
    receipts.append(summary)
    releasable = False
    started = time.monotonic()
    try:
        deadline = min(lease_deadline, int(time.time() * 1000) + budget_ms)
        raw = bytearray()
        async with asyncio.timeout(14):
            async with client.stream('POST', engine.endpoint, json=body, timeout=None,
                    headers={'x-harmony-execution-id': nonce,
                             'x-harmony-execution-deadline-ms': str(deadline)}) as response:
                status = response.status_code
                summary['status'] = status
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_BYTES:
                        raise RuntimeError('receipt exceeds 64 KiB; reservation retained')
                    raw.extend(chunk)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RuntimeError('non-object receipt; reservation retained')
        receipt = value.get('harmony_execution')
        completed = (status == 200 and value.get('id') == identity
                     and isinstance(receipt, dict) and receipt.get('request_id') == identity
                     and receipt.get('completion_confirmed') is True)
        correlated = value.get('request_id') == identity
        stopped = (correlated and value.get('execution_started') is True
                   and value.get('cessation_confirmed') is True)
        not_started = correlated and value.get('execution_started') is False
        known_refusal = value.get('cause') in CAUSE_HTTP and CAUSE_HTTP[value['cause']] == status
        releasable = completed or (known_refusal and (stopped or not_started))
        summary.update({
                   'elapsed_ms': round((time.monotonic() - started) * 1000),
                   'completion_confirmed': completed, 'cessation_confirmed': stopped,
                   'execution_started': value.get('execution_started'),
                   'cause': str(value.get('cause', ''))[:128]})
        if name == 'deadline':
            if not (status == 504 and value.get('cause') == 'deadline_exceeded' and stopped):
                raise RuntimeError('deadline control did not prove started native work stopped: ' + json.dumps(summary))
        else:
            if not completed:
                raise RuntimeError('success control lacks correlated completion: ' + json.dumps(summary))
            choices = value.get('choices', [])
            if len(choices) != 1 or choices[0].get('finish_reason') != 'stop':
                raise RuntimeError('success control returned incomplete generation')
            answer = json.loads(choices[0]['message']['content'])
            validate_extraction(answer)
            if answer.get('outcome') != 'ask' or answer.get('request_quote') not in QUESTION:
                raise RuntimeError('success control did not extract its current grounded question')
        return summary
    finally:
        # No socket error, timeout, process exit or uncorrelated reply can
        # discharge this real reservation. Stop the run on uncertainty.
        if releasable:
            await fleet.finish(grant)
            summary['lease_released'] = True


async def verify(args, result):
    config = json.loads(private_bytes(args.config, 8192))
    engine = QualifiedEngine(**config['engine'])
    engine.validate()
    if engine.profile_id != FLASH_NEXT_PROFILE_ID or engine.backend != 'cuda':
        raise RuntimeError('verification requires the installed Flash Next CUDA profile')
    waiver = (private_bytes(config['operatorWaiverFile'], 8192)
              if config.get('operatorWaiverFile') else None)
    validate_registry_profile(private_bytes(config['profileFile'], MAX_BYTES),
                              config['profileSha256'], engine, operator_waiver_raw=waiver)
    execution_token = private_bytes(config['executionTokenFile'], 256).decode().strip()
    broker_token = private_bytes(config['brokerTokenFile'], 2048).decode().strip()
    if len(execution_token) < 32 or not broker_token:
        raise RuntimeError('configured credentials are incomplete')
    async with httpx.AsyncClient(headers={'authorization': 'Bearer ' + execution_token},
            limits=httpx.Limits(max_connections=1)) as execution_client:
        # Reuse the serving transport's URL restrictions, without replacing
        # raw receipts by its translated error (we need started+stopped proof).
        ProtectedExecutionTransport(client=execution_client, endpoint=engine.endpoint)
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=2)) as broker_client:
            fleet = FleetExtractionAdmission(client=broker_client, broker_url=config['brokerUrl'],
                token=broker_token, engine=engine, target_id=config['targetId'], host_id=config['hostId'])
            result['engine'] = {k: getattr(engine, k) for k in
                ('unit', 'profile_id', 'model_revision', 'tokenizer_hash', 'backend')}
            for name, budget in [('before', 8000), ('deadline', args.cancel_budget_ms), ('after', 8000)]:
                await control(name, client=execution_client, fleet=fleet,
                    engine=engine, owner=args.owner, budget_ms=budget, receipts=result['controls'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/harmony/pane-ask-runtime.json')
    parser.add_argument('--owner', required=True, help='Owner namespace accepted by this configured service principal')
    parser.add_argument('--output', required=True, help='New private receipt file; never overwritten')
    parser.add_argument('--cancel-budget-ms', type=int, default=500, choices=range(100, 2001), metavar='100..2000')
    args = parser.parse_args()
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    result = {'ok': False, 'scope': 'real admitted execution cessation only',
              'model_quality_qualified': False, 'controls': []}
    try:
        asyncio.run(verify(args, result))
        result['ok'] = True
    except Exception as exc:
        # Never serialize configuration, response contents or HTTP headers.
        result['error'] = type(exc).__name__ + ': ' + str(exc)[:2048]
    finally:
        raw = (json.dumps(result, indent=2) + '\n').encode()
        if len(raw) > MAX_BYTES:
            raw = b'{"ok":false,"error":"verification receipt exceeded 64 KiB"}\n'
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
    print(json.dumps({'ok': result['ok'], 'output': args.output, 'model_quality_qualified': False}))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
