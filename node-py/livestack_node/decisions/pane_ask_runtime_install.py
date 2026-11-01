"""Install only an operator-qualified, pinned local extraction runtime.

Configuration and credential files are private and bounded. The committed
unqualified profile is deliberately ineligible. This module creates no
qualification artifacts and cannot promote an engine from source counts.
"""
import hashlib
import json
import logging
from urllib.parse import urlsplit

import httpx

from .contract import ContractError
from .extraction_contract import PROFILE_ID
from .fleet_extraction_admission import FleetExtractionAdmission
from .nominal_27b_adapter import Nominal27BExtractionAdapter, QualifiedEngine
from .operator_waiver import validate_operator_waiver
from .pane_ask_ingress import PaneAskRuntime
from .protected_execution_transport import ProtectedExecutionTransport
from .vllm_extraction_endpoint import private_bytes


def validate_registry_profile(raw, expected_sha256, engine, *, operator_waiver_raw=None):
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ContractError('profile_unavailable', 'extraction profile artifact digest differs from registry', 503)
    try:
        profile = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ContractError('profile_unavailable', 'extraction registry profile is invalid JSON', 503) from exc
    waiver = (validate_operator_waiver(operator_waiver_raw, profile_id=PROFILE_ID, engine=engine)
              if operator_waiver_raw is not None else None)
    if (not isinstance(profile, dict) or profile.get('profile_id') != PROFILE_ID
            or (PROFILE_ID.endswith(':unqualified') and waiver is None)
            or (engine.qualification_hash.startswith('operator-waiver:') and waiver is None)
            or profile.get('qualification_hash') != engine.qualification_hash
            or profile.get('calibration_hash') != engine.qualification_hash
            or profile.get('qualification_hash') in (None, '', 'unqualified')
            or not isinstance(profile.get('physical_kinds'), list)
            or engine.unit not in profile['physical_kinds']):
        raise ContractError('profile_unavailable', 'extraction profile has no matching qualified physical implementation', 503)
    engine.validate()
    return profile


async def configure_pane_ask_runtime(app, *, host_id, device_id, units,
                                     config_path='/etc/harmony/pane-ask-runtime.json'):
    """Return owned clients for shutdown, or leave the route explicitly disabled."""
    app.state.pane_ask_runtime = None
    app.state.pane_ask_runtime_refusal = None
    clients = []
    phase = 'private configuration'
    try:
        config = json.loads(private_bytes(config_path, 8192))
        keys = {'version', 'engine', 'profileFile', 'profileSha256', 'brokerUrl',
                'brokerTokenFile', 'executionTokenFile', 'targetId', 'hostId'}
        if (not isinstance(config, dict) or not keys <= set(config)
                or set(config) - keys - {'operatorWaiverFile'}
                or type(config['version']) is not int or config['version'] != 1):
            raise ContractError('profile_unavailable', 'invalid extraction runtime configuration schema', 503)
        if not isinstance(config['engine'], dict):
            raise ContractError('profile_unavailable', 'extraction engine registry identity is missing', 503)
        try:
            engine = QualifiedEngine(**config['engine'])
        except TypeError as exc:
            raise ContractError('profile_unavailable', 'invalid extraction engine registry fields', 503) from exc
        if engine.unit not in units or engine.device_id != device_id or config['hostId'] != host_id or engine.backend != 'cuda':
            raise ContractError('profile_unavailable', 'extraction registry target differs from this local CUDA node', 503)
        phase = 'profile digest and qualification'
        waiver_raw = (private_bytes(config['operatorWaiverFile'], 8192)
                      if 'operatorWaiverFile' in config else None)
        validate_registry_profile(private_bytes(config['profileFile'], 65536), config['profileSha256'], engine,
                                  operator_waiver_raw=waiver_raw)
        broker = urlsplit(config['brokerUrl'])
        if broker.scheme not in ('http', 'https') or not broker.hostname or broker.username or broker.password or broker.query or broker.fragment:
            raise ContractError('profile_unavailable', 'fleet broker URL must contain no credential or request parameters', 503)
        # Only after profile/target verification do we load any credential.
        phase = 'service credentials'
        broker_token = private_bytes(config['brokerTokenFile'], 2048).decode().strip()
        execution_token = private_bytes(config['executionTokenFile'], 256).decode().strip()
        if not broker_token or len(execution_token) < 32 or any(c.isspace() for c in execution_token):
            raise ContractError('profile_unavailable', 'extraction service credentials are incomplete', 503)
        phase = 'protected execution transport'
        engine_client = httpx.AsyncClient(headers={'authorization': 'Bearer ' + execution_token},
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))
        clients.append(engine_client)
        transport = ProtectedExecutionTransport(client=engine_client, endpoint=engine.endpoint)
        phase = 'strict fleet callbacks'
        fleet_client = httpx.AsyncClient(limits=httpx.Limits(max_connections=2, max_keepalive_connections=2))
        clients.append(fleet_client)
        fleet = FleetExtractionAdmission(client=fleet_client, broker_url=config['brokerUrl'],
            token=broker_token, engine=engine, target_id=config['targetId'], host_id=host_id)
        app.state.pane_ask_runtime = PaneAskRuntime(
            Nominal27BExtractionAdapter(engine, client=engine_client, execution_transport=transport),
            fleet.admit, fleet.finish)
        logging.warning('extraction runtime installed; acceptance=%s',
                        'operator-waived internal testing' if waiver_raw is not None else 'measured qualification')
        return clients
    except Exception:
        app.state.pane_ask_runtime = None
        app.state.pane_ask_runtime_refusal = {'cause': 'profile_unavailable',
            'detail': 'extraction runtime prerequisites refused: ' + phase}
        for client in clients:
            await client.aclose()
        logging.error('extraction runtime unavailable; prerequisite phase=%s', phase)
        return []


def register_pane_ask_runtime_installer(app, *, host_id, device_id, units):
    clients = []  # At most the two clients returned by the single startup hook.

    @app.on_event('startup')
    async def install():
        if clients:
            raise RuntimeError('extraction runtime already owns its bounded client set')
        clients.extend(await configure_pane_ask_runtime(app,
            host_id=host_id, device_id=device_id, units=units))

    @app.on_event('shutdown')
    async def close():
        app.state.pane_ask_runtime = None
        for client in clients:
            await client.aclose()
        clients.clear()
