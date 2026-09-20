"""A.5: one journal line per mutating request and per auth refusal.

The R.3 gate is "zero 401s from an address not in the inventory over 24 h" —
verifiable only if the journal says WHO asked and WHERE from. These pin the
contract: refusals and writes are logged with source, method, path, status
and principal name; reads stay silent; and the token itself NEVER appears in
any emitted line.
"""
import json
import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from livestack_node import attach  # noqa: E402
from livestack_node import ManagedUnit, ResidencyPolicy, noop_free  # noqa: E402
from livestack_node.fleet_auth import load_principals  # noqa: E402
from livestack_node.hostd import build_app  # noqa: E402

TOK_ATTUNE = "a" * 40
TOK_BENCHDAY = "b" * 40

HOSTD_PRINCIPALS = load_principals(
    '{"%s": {"name": "attune", "owner": "attune"},'
    ' "%s": {"name": "benchday-hub", "delegate_prefix": "benchday:"}}'
    % (TOK_ATTUNE, TOK_BENCHDAY))


def _hostd_client(monkeypatch, capsys):
    monkeypatch.setenv("LIVESTACK_REPLAN_INTERVAL", "0")

    class _Broker:
        peers = []
        fleet_principals = HOSTD_PRINCIPALS
        fleet_policy = None

        def __init__(self):
            self.planned = []

        def plan_and_apply(self, requests, _):
            self.planned.extend(requests)

            class _P:
                def of(self, cls):
                    return []

                def summary(self):
                    return {}
            return _P()

        def emit_admit(self, *a, **k):
            pass

        def hosted_checkout(self, *a, **k):
            return None

        def device_config(self):
            return {}

    capsys.readouterr()                    # drop startup noise
    client = TestClient(build_app(_Broker()), raise_server_exceptions=False)
    return client


def _facade_client(tmp_path, monkeypatch, capsys):
    tokens = tmp_path / "node-tokens.json"
    tokens.write_text(json.dumps(
        {TOK_BENCHDAY: {"name": "benchday-hub", "owner": "benchday:ops"}}))
    os.chmod(tokens, 0o600)
    monkeypatch.setenv("LIVESTACK_NODE_TOKENS_FILE", str(tokens))
    app = FastAPI()
    attach(app, host_id="h", kind="llm",
           units={"llm": ManagedUnit("llm", loader=lambda: "m", freer=noop_free,
                                     residency_policy=ResidencyPolicy.UNPINNED)},
           idle_seconds=120, coload=True, gpu_call=lambda fn: fn(),
           device_meter=None)
    capsys.readouterr()
    return TestClient(app)


def _audit_lines(capsys):
    out = capsys.readouterr().out
    return [l for l in out.splitlines() if l.startswith("[audit]")]


# -- the gate's evidence -------------------------------------------------------

def test_a_tokenless_mutating_call_is_logged_with_source_and_401(
        tmp_path, monkeypatch, capsys):
    client = _facade_client(tmp_path, monkeypatch, capsys)
    r = client.post("/livestack/model/evict", json={"unit": "llm"})
    assert r.status_code == 401
    lines = _audit_lines(capsys)
    assert len(lines) == 1
    line = lines[0]
    assert "method=POST" in line
    assert "path=/livestack/model/evict" in line
    assert "status=401" in line
    assert "src=" in line, "the source address the gate counts"
    assert "principal=-" in line, "no credential: anonymous, said explicitly"


def test_an_admit_with_a_valid_token_names_the_principal(monkeypatch, capsys):
    client = _hostd_client(monkeypatch, capsys)
    r = client.post("/admit", json={"kind": "qwen", "owner": "attune"},
                    headers={"Authorization": f"Bearer {TOK_ATTUNE}"})
    assert r.status_code == 200
    lines = _audit_lines(capsys)
    assert len(lines) == 1
    assert "principal=attune" in lines[0]
    assert "method=POST" in lines[0] and "path=/admit" in lines[0]
    assert "status=200" in lines[0]


def test_a_rejected_token_is_represented_by_its_fingerprint(monkeypatch, capsys):
    client = _hostd_client(monkeypatch, capsys)
    r = client.post("/admit", json={"kind": "qwen"},
                    headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401
    lines = _audit_lines(capsys)
    assert len(lines) == 1
    assert "principal=unknown(" in lines[0], \
        "an unknown caller is what the inventory gate counts"


def test_the_token_value_never_appears_in_any_log_line(
        tmp_path, monkeypatch, capsys):
    client = _facade_client(tmp_path, monkeypatch, capsys)
    client.post("/livestack/model/evict", json={"unit": "llm"})
    client.post("/livestack/model/evict", json={"unit": "llm"},
                headers={"Authorization": f"Bearer {TOK_ATTUNE}"})
    client.post("/livestack/lease", json={"kind": "llm"},
                headers={"Authorization": f"Bearer {TOK_BENCHDAY}"})
    out = capsys.readouterr().out
    assert TOK_ATTUNE not in out
    assert TOK_BENCHDAY not in out
    assert "Bearer" not in out
    # The lines themselves were emitted.
    assert out.count("[audit]") == 3


def test_reads_stay_silent_at_info(tmp_path, monkeypatch, capsys):
    client = _facade_client(tmp_path, monkeypatch, capsys)
    assert client.get("/livestack/health").status_code == 200
    assert client.get("/livestack/residence").status_code == 200
    assert client.get("/livestack/capability").status_code == 200
    assert _audit_lines(capsys) == [], \
        "polls are not audit events — the gate needs writes and refusals"


def test_a_mutating_call_with_a_wrong_token_is_one_line_not_two(
        tmp_path, monkeypatch, capsys):
    # A refused write matches both rules (mutating AND 401); it must still
    # cost exactly one line — a request is one event.
    client = _facade_client(tmp_path, monkeypatch, capsys)
    client.post("/livestack/model/warm", json={"unit": "llm"},
                headers={"Authorization": "Bearer wrong"})
    lines = _audit_lines(capsys)
    assert len(lines) == 1
    assert "status=401" in lines[0] and "path=/livestack/model/warm" in lines[0]
