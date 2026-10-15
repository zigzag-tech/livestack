"""Opt-in, loopback-only vLLM endpoint with bounded tracked execution.

Requires an operator-owned private configuration and token file. This private
engine control is not a public decision route or a qualification receipt.
The facade still owns fleet admission and the resident execution scope.
"""
from dataclasses import dataclass, field
import asyncio
import hmac
import json
import logging
import os
import re
import stat
import time

from fastapi.responses import JSONResponse
from fastapi import Request

from .contract import ContractError, MAX_BODY_BYTES
from .execution_uncertain import ExecutionUnconfirmed
from .tracked_extraction_execution import TrackedExtractionExecution
from .vllm_execution_barrier import validate_execution_configuration


def private_bytes(path, maximum):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("private execution configuration requires absolute paths")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o077:
            raise ValueError("execution configuration must be an owned private regular file")
        raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("private execution configuration exceeds its bound")
        return raw


@dataclass(frozen=True)
class ExecutionEndpointRuntime:
    token: str
    model: str
    control: TrackedExtractionExecution
    serve_chat: object
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)


class ExtractionEndpointPlugin:
    name = 'harmony_extraction_execution'
    required_tasks = ('generate',)

    def attach_router(self, app):
        @app.post('/harmony/extraction')
        async def extract(request: Request):
            runtime = getattr(request.app.state, 'harmony_extraction_execution', None)
            identity = ''
            started = False
            began = time.monotonic()
            acquired = False
            try:
                if not isinstance(runtime, ExecutionEndpointRuntime):
                    raise ContractError('profile_unavailable', 'protected engine execution is not configured', 503)
                if request.client is None or request.client.host not in ('127.0.0.1', '::1'):
                    raise ContractError('forbidden', 'engine execution control requires loopback', 403)
                presented = request.headers.get('authorization', '')
                if not hmac.compare_digest(presented.encode(), ('Bearer ' + runtime.token).encode()):
                    raise ContractError('unauthorized', 'engine execution credential required', 401)
                nonce = request.headers.get('x-harmony-execution-id', '')
                if not re.fullmatch(r'[0-9a-f]{32}', nonce):
                    raise ContractError('invalid_input', 'engine execution requires a bounded caller nonce', 400)
                identity = 'chatcmpl-' + nonce
                supplied_deadline = request.headers.get('x-harmony-execution-deadline-ms', '')
                if not re.fullmatch(r'[0-9]{13}', supplied_deadline):
                    raise ContractError('invalid_input', 'engine execution requires an absolute deadline', 400)
                budget = (int(supplied_deadline) - int(time.time() * 1000)) / 1000
                if budget <= 0:
                    raise ContractError('deadline_exceeded', 'engine execution already expired', 504)
                if budget > 8:
                    raise ContractError('invalid_input', 'engine execution deadline exceeds eight seconds', 400)
                expires = began + budget
                if runtime.gate.locked():
                    raise ContractError('capacity', 'engine execution ingress slot is occupied', 429)
                await runtime.gate.acquire()
                acquired = True
                raw = bytearray()
                try:
                    async with asyncio.timeout(max(0, expires - time.monotonic())):
                        async for chunk in request.stream():
                            if len(raw) + len(chunk) > MAX_BODY_BYTES:
                                raise ContractError('invalid_input', 'engine execution body exceeds 64 KiB', 400)
                            raw.extend(chunk)
                except TimeoutError as exc:
                    raise ContractError('deadline_exceeded', 'engine execution body timed out', 504) from exc
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise ContractError('invalid_input', 'engine execution body must be JSON', 400) from exc
                expected = {'model', 'stream', 'temperature', 'max_tokens', 'chat_template_kwargs', 'messages'}
                if (not isinstance(body, dict) or set(body) != expected
                        or body['model'] != runtime.model or body['stream'] is not False
                        or type(body['temperature']) not in (int, float) or body['temperature'] != 0
                        or type(body['max_tokens']) is not int or body['max_tokens'] != 1024
                        or body['chat_template_kwargs'] != {'enable_thinking': False}
                        or not isinstance(body['messages'], list) or len(body['messages']) != 2
                        or any(not isinstance(m, dict) or set(m) != {'role', 'content'}
                               or m['role'] != role or not isinstance(m['content'], str)
                               for m, role in zip(body['messages'], ('system', 'user')))):
                    raise ContractError('invalid_input', 'engine execution requires the fixed extraction policy', 400)

                async def start(tracked_id):
                    nonlocal started
                    started = True
                    # None prevents an HTTP X-Request-Id overriding our tracked id.
                    from vllm.entrypoints.openai.chat_completion.protocol import ChatCompletionRequest, ChatCompletionResponse
                    chat = ChatCompletionRequest.model_validate({**body, 'request_id': tracked_id.removeprefix('chatcmpl-')})
                    response = await runtime.serve_chat.create_chat_completion(chat, None)
                    if not isinstance(response, ChatCompletionResponse) or response.id != tracked_id:
                        raise ContractError('invalid_output', 'engine returned no correlated completion', 422)
                    return response.model_dump(mode='json')

                remaining = expires - time.monotonic()
                if remaining <= 0:
                    raise ContractError('deadline_exceeded', 'engine execution expired before dispatch', 504)
                value = await runtime.control.execute(identity, start, timeout_seconds=remaining)
                value['harmony_execution'] = {'request_id': identity, 'completion_confirmed': True}
                if len(json.dumps(value).encode()) > MAX_BODY_BYTES:
                    raise ContractError('invalid_output', 'complete engine response exceeds 64 KiB', 422)
                return JSONResponse(value)
            except ContractError as exc:
                return JSONResponse({**exc.envelope(identity), 'execution_started': started,
                    'cessation_confirmed': started and not isinstance(exc, ExecutionUnconfirmed)
                        and runtime.control.active is None}, status_code=exc.http_status)
            except Exception:
                logging.error('protected extraction execution failed; result refused')
                return JSONResponse({**ContractError('unhealthy', 'protected engine execution failed', 503).envelope(identity),
                    'execution_started': started, 'cessation_confirmed': started and runtime.control.active is None}, status_code=503)
            finally:
                if acquired:
                    runtime.gate.release()

    async def init_state(self, engine_client, state, args):
        del args
        path = '/etc/harmony/extraction-execution.json'
        phase = 'private configuration'
        try:
            config = json.loads(private_bytes(path, 4096))
            if not isinstance(config, dict) or set(config) != {'version', 'tokenFile', 'model'} or type(config['version']) is not int or config['version'] != 1:
                raise ValueError('invalid execution configuration schema')
            phase = 'private credential'
            token = private_bytes(config['tokenFile'], 256).decode().strip()
            if len(token) < 32 or any(c.isspace() for c in token):
                raise ValueError('invalid private execution credential')
            model = config['model']
            if not isinstance(model, str) or not model or len(model) > 256:
                raise ValueError('invalid engine model identity')
            phase = 'engine execution configuration'
            core = getattr(engine_client, 'engine_core', None)
            engine_config = getattr(engine_client, 'vllm_config', None)
            logging.info('protected extraction engine control: core=%s async=%s batches=%s',
                type(core).__name__,
                getattr(getattr(engine_config, 'scheduler_config', None), 'async_scheduling', None),
                getattr(engine_config, 'max_concurrent_batches', None))
            validate_execution_configuration(engine_client)
            phase = 'base model alias'
            # ModelConfig collapses aliases to its first name for metrics.
            # The native serving registry retains every base alias and excludes
            # LoRA adapters, so use the same membership check as native chat.
            models = getattr(state, 'openai_serving_models', None)
            is_base_model = getattr(models, 'is_base_model', None)
            if not callable(is_base_model) or is_base_model(model) is not True:
                raise ValueError('execution model must be a base engine alias')
            phase = 'native chat serving handler'
            serving = getattr(state, 'openai_serving_chat', None)
            if serving is None:
                raise ValueError('chat serving handler is unavailable')
            state.harmony_extraction_execution = ExecutionEndpointRuntime(
                token, model, TrackedExtractionExecution(engine_client), serving)
            logging.info('protected extraction execution endpoint configured')
        except (OSError, ValueError, ContractError):
            state.harmony_extraction_execution = None
            logging.error('protected extraction execution endpoint unavailable; prerequisite phase=%s', phase)
