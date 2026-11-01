"""Actual shipped profile/configuration refusals, not model qualification.

The negative control uses the packaged unqualified profile, not a fabricated
qualified registry. Successful production installation still requires genuine
qualification, matching immutable artifacts and live broker/GPU readback.
"""
import asyncio
import hashlib
import json
from dataclasses import replace

from fastapi import FastAPI
import pytest

from livestack_node.decisions.contract import ContractError
from livestack_node.decisions.nominal_27b_adapter import QualifiedEngine
from livestack_node.decisions.pane_ask_runtime_install import configure_pane_ask_runtime, validate_registry_profile
from livestack_node.decisions.paths import DATA_DIR


def engine():
    return QualifiedEngine('llm_general', 'http://127.0.0.1:8189/harmony/extraction',
        'device-control', 'cuda', 'fixture-model', 'fixture-tokenizer', 'fixture-qualification', 'int4', 27)


def test_real_shipped_unqualified_profile_cannot_install_even_with_matching_digest():
    raw = (DATA_DIR/'profiles'/'pane-ask-27b-v1.json').read_bytes()
    with pytest.raises(ContractError, match='no matching qualified physical implementation'):
        validate_registry_profile(raw, hashlib.sha256(raw).hexdigest(), engine())


def test_artifact_digest_mismatch_has_distinct_refusal():
    raw = (DATA_DIR/'profiles'/'pane-ask-27b-v1.json').read_bytes()
    with pytest.raises(ContractError, match='digest differs'):
        validate_registry_profile(raw, '0'*64, engine())


def test_unqualified_profile_refuses_before_loading_any_credential(tmp_path):
    profile = tmp_path/'profile.json'
    profile.write_bytes((DATA_DIR/'profiles'/'pane-ask-27b-v1.json').read_bytes());profile.chmod(0o600)
    config = {'version':1,'engine':engine().__dict__, 'profileFile':str(profile),
        'profileSha256':hashlib.sha256(profile.read_bytes()).hexdigest(),
        'brokerUrl':'http://127.0.0.1:8799','brokerTokenFile':str(tmp_path/'missing-broker-token'),
        'executionTokenFile':str(tmp_path/'missing-execution-token'),
        'targetId':'target-control','hostId':'host-control'}
    path = tmp_path/'runtime.json';path.write_text(json.dumps(config));path.chmod(0o600)
    app = FastAPI()
    assert asyncio.run(configure_pane_ask_runtime(app,host_id='host-control',
        device_id='device-control',units={'llm_general':object()},config_path=str(path))) == []
    assert app.state.pane_ask_runtime is None
    assert app.state.pane_ask_runtime_refusal['detail'] == 'extraction runtime prerequisites refused: profile digest and qualification'


def waived_artifacts():
    candidate = engine()
    waiver = json.dumps({'version': 1, 'decision': 'waive-qualification',
        'scope': 'internal-testing', 'operator': 'settinghead',
        'profile_id': 'pane-ask-27b-v1:unqualified', 'unit': candidate.unit,
        'model_revision': candidate.model_revision, 'tokenizer_hash': candidate.tokenizer_hash,
        'reason': 'Explicit operator acceptance; no measured qualification claimed.'}).encode()
    marker = 'operator-waiver:sha256:' + hashlib.sha256(waiver).hexdigest()
    candidate = replace(candidate, qualification_hash=marker)
    profile = json.loads((DATA_DIR/'profiles'/'pane-ask-27b-v1.json').read_bytes())
    profile.update(qualification_hash=marker, calibration_hash=marker, physical_kinds=[candidate.unit])
    return candidate, waiver, json.dumps(profile).encode()


def test_operator_marker_alone_cannot_bypass_private_receipt():
    candidate, waiver, raw = waived_artifacts()
    with pytest.raises(ContractError):
        validate_registry_profile(raw, hashlib.sha256(raw).hexdigest(), candidate)
    with pytest.raises(ContractError, match='does not match engine identity'):
        validate_registry_profile(raw, hashlib.sha256(raw).hexdigest(),
            replace(candidate, model_revision='different-model'), operator_waiver_raw=waiver)


def test_private_operator_waiver_installs_clients_without_claiming_qualification(tmp_path):
    candidate, waiver, profile = waived_artifacts()
    def private(name, raw):
        path = tmp_path/name; path.write_bytes(raw); path.chmod(0o600); return str(path)
    config = {'version': 1, 'engine': candidate.__dict__,
        'profileFile': private('profile.json', profile),
        'profileSha256': hashlib.sha256(profile).hexdigest(),
        'operatorWaiverFile': private('waiver.json', waiver),
        'brokerUrl': 'http://127.0.0.1:8799',
        'brokerTokenFile': private('broker.token', b'private-broker-control'),
        'executionTokenFile': private('execution.token', b'x'*48),
        'targetId': 'target-control', 'hostId': 'host-control'}
    path = private('runtime.json', json.dumps(config).encode())
    async def run():
        app = FastAPI()
        clients = await configure_pane_ask_runtime(app, host_id='host-control',
            device_id='device-control', units={'llm_general': object()}, config_path=path)
        assert len(clients) == 2
        assert app.state.pane_ask_runtime is not None
        assert app.state.pane_ask_runtime.adapter.engine.qualification_hash.startswith('operator-waiver:sha256:')
        for client in clients:
            await client.aclose()
    asyncio.run(run())
