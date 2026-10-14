"""Bounded facade transport for the authenticated private execution endpoint.

The caller supplies a client configured from private credential settings; no
credential is accepted in a URL or request body. A socket closure is never a
cessation receipt. This transport installs no runtime and qualifies no model.
"""
import asyncio
import json
import secrets
from urllib.parse import urlsplit

import httpx

from .contract import ContractError, CAUSE_HTTP, MAX_BODY_BYTES
from .execution_uncertain import ExecutionUnconfirmed


class ProtectedExecutionTransport:
    def __init__(self, *, client, endpoint):
        url = urlsplit(endpoint)
        if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', '::1')
                or url.username or url.password or url.path != '/harmony/extraction'
                or url.query or url.fragment):
            raise ContractError('profile_unavailable', 'protected execution requires its local private endpoint', 503)
        self.client, self.endpoint = client, endpoint

    async def generate(self, body, *, deadline_at_ms):
        nonce = secrets.token_hex(16)
        identity = 'chatcmpl-' + nonce
        try:
            # The private engine owns an eight-second deadline plus two bounded
            # two-second cessation steps. A lost frontend still cannot hang the
            # facade indefinitely or release its reservations on timeout.
            async with asyncio.timeout(14):
                async with self.client.stream('POST', self.endpoint, json=body,
                        headers={'x-harmony-execution-id': nonce,
                                 'x-harmony-execution-deadline-ms': str(deadline_at_ms)}, timeout=None) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > MAX_BODY_BYTES:
                            raise ExecutionUnconfirmed('unhealthy', 'execution receipt exceeds 64 KiB; reservations retained', 503)
                        raw.extend(chunk)
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError) as exc:
                        raise ExecutionUnconfirmed('unhealthy', 'execution receipt is not JSON; reservations retained', 503) from exc
                    if not isinstance(value, dict):
                        raise ExecutionUnconfirmed('unhealthy', 'execution receipt is not an object; reservations retained', 503)
                    if response.status_code >= 400:
                        correlated = value.get('request_id') == identity
                        stopped = value.get('execution_started') is True and value.get('cessation_confirmed') is True
                        not_started = value.get('execution_started') is False
                        cause = value.get('cause')
                        if correlated and (stopped or not_started) and cause in CAUSE_HTTP and CAUSE_HTTP[cause] == response.status_code:
                            raise ContractError(cause, 'protected engine refused after verified non-execution or cessation', response.status_code)
                        raise ExecutionUnconfirmed('unhealthy', 'private engine returned no correlated cessation evidence; reservations retained', 503)
                    if response.status_code != 200:
                        raise ExecutionUnconfirmed('unhealthy', 'private execution returned an unexpected status; reservations retained', 503)
                    receipt = value.get('harmony_execution')
                    if (not isinstance(receipt, dict) or receipt.get('request_id') != identity
                            or receipt.get('completion_confirmed') is not True or value.get('id') != identity):
                        raise ExecutionUnconfirmed('unhealthy', 'private engine completion is uncorrelated; reservations retained', 503)
                    return value
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ExecutionUnconfirmed('unhealthy', 'private execution transport failed without cessation evidence; reservations retained', 503) from exc
