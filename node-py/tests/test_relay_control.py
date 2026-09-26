"""Tests for relay_control.py (openspec meshlink-transport-backbone task 7.1).

The load-bearing tests here mint tokens in PYTHON and verify them with the
REAL meshlink relay package (``verifyRelayAttachment`` /
``verifySpeechRelayCapability`` from the built ``dist/``, driven over node via
subprocess) — the same two-halves-must-agree discipline as meshlink's own
control.test.ts. A Python-only reimplementation of the relay's verify would
only prove the port agrees with itself.

Tokens are protocol, so the negative cases matter as much as the positive
ones: wrong realm, wrong key, expired, tampered must all be refused by the
relay itself, not by our local mirror.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from livestack_node import relay_control as rc

REPO_ROOT = Path(__file__).resolve().parents[2]
MESHLINK_REPO = Path(os.environ.get("MESHLINK_REPO") or REPO_ROOT.parent / "meshlink")
MESHLINK_DIST = MESHLINK_REPO / "packages" / "mesh_relay" / "dist"

NODE_HARNESS = r"""
// Minted-by-Python -> verified-by-the-real-relay harness.
// Reads one JSON job on stdin, writes one JSON result on stdout.
//   {op: "verify_attachment", args: {token, publicKeyB64, relayId, realm, aud?}}
//   {op: "verify_capability", args: {token, keys: [{kid, secret}], targetId,
//        relayId?, scope, typ?, aud?, now?}}
const job = JSON.parse(await new Promise((res, rej) => {
  let s = ""; process.stdin.on("data", (d) => (s += d));
  process.stdin.on("end", () => res(s)); process.stdin.on("error", rej);
}));
const dist = job.dist;
let out;
if (job.op === "verify_attachment") {
  const { verifyRelayAttachment } = await import(dist + "/attachment.js");
  const a = job.args;
  const grant = verifyRelayAttachment(a.token, a.publicKeyB64, a.relayId, a.realm, a.aud);
  out = { grant };
} else if (job.op === "verify_capability") {
  const { verifySpeechRelayCapability } = await import(dist + "/capability.js");
  const a = job.args;
  out = verifySpeechRelayCapability({ ...a, keys: a.keys });
} else {
  out = { error: "unknown op " + job.op };
}
process.stdout.write(JSON.stringify(out));
"""

RELAY_ID = "relay-near"
OTHER_RELAY_ID = "relay-far"
REALM = "livestack"
OTHER_REALM = "benchday"
NOW = 1_760_000_000

# The mesh_relay test suite's own shapes (control.test.ts INPUT): one hub key
# mints, one daemon key is the attaching identity.
ATTACH_KEY_PEM = Ed25519PrivateKey.generate()
DAEMON_KEY = Ed25519PrivateKey.generate()


def _daemon_key_b64(key: Ed25519PrivateKey = DAEMON_KEY) -> str:
    return base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def _hub_pub_b64() -> str:
    return rc.attachment_public_key_b64(ATTACH_KEY_PEM)


def _mint_attachment(**overrides):
    # Attachment verify has no injectable clock on the relay side, so these
    # must be minted against the REAL wall clock (capabilities pass `now`).
    args = dict(realm=REALM, daemon_id="gpu-box-7",
                daemon_key_b64=_daemon_key_b64(), relay_id=RELAY_ID,
                account_id="acct_livestack", now=int(time.time()))
    args.update(overrides)
    return rc.mint_attachment(ATTACH_KEY_PEM, **args)


def _mint_capability(ring=None, **overrides):
    ring = ring or rc.CapKeyRing([rc.CapKey(kid="k1", secret="secret-one")])
    args = dict(account_id="acct_livestack", device_id="node-1",
                target_id="target-a", relay_id=RELAY_ID, region="local",
                scopes=["terminal.proxy"], now=NOW)
    args.update(overrides)
    return rc.mint_capability(ring, **args)


def _relay(op: str, args: dict) -> dict:
    """Drive the real mesh_relay verify code over node. Skips (never silently
    passes) when the meshlink checkout is not beside this repo."""
    if not (MESHLINK_DIST / (op.replace("verify_", "") + ".js")).exists():
        pytest.skip(f"meshlink relay dist not built at {MESHLINK_DIST}")
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    job = json.dumps({"op": op, "args": args, "dist": str(MESHLINK_DIST)})
    proc = subprocess.run(
        ["node", "--input-type=module", "--eval", NODE_HARNESS],
        input=job, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# clamp_ttl / seconds_until_refresh
# ---------------------------------------------------------------------------

def test_clamp_ttl_matches_relay_bounds():
    assert rc.clamp_ttl(10) == 30           # floor
    assert rc.clamp_ttl(100_000) == 900     # ceiling (15 min)
    assert rc.clamp_ttl(300) == 300
    assert rc.clamp_ttl(0) == rc.DEFAULT_CAPABILITY_TTL_SECONDS
    assert rc.clamp_ttl(-5) == rc.DEFAULT_CAPABILITY_TTL_SECONDS
    assert rc.clamp_ttl(float("nan")) == rc.DEFAULT_CAPABILITY_TTL_SECONDS


def test_seconds_until_refresh():
    exp = NOW + 300
    assert rc.seconds_until_refresh(exp, now=NOW, lead=60) == 240
    assert rc.seconds_until_refresh(exp, now=exp - 60, lead=60) == 0
    assert rc.seconds_until_refresh(exp, now=exp, lead=60) == 0  # never negative


# ---------------------------------------------------------------------------
# bdrt1 attachments, verified by the REAL relay
# ---------------------------------------------------------------------------

def test_attachment_minted_in_python_verified_by_real_relay():
    token, claims = _mint_attachment()
    assert token.startswith("bdrt1.")
    assert claims["exp"] - claims["iat"] == 300  # default TTL, real clock

    grant = _relay("verify_attachment", {
        "token": token, "publicKeyB64": _hub_pub_b64(),
        "relayId": RELAY_ID, "realm": REALM, "aud": rc.DEFAULT_ATTACHMENT_AUDIENCE,
    })["grant"]
    assert grant is not None, "the relay it names must accept it"
    assert grant["daemon_id"] == "gpu-box-7"
    assert grant["account_id"] == "acct_livestack"
    assert grant["realm"] == REALM


def test_attachment_relay_verifies_for_no_other_relay_or_realm():
    token, _ = _mint_attachment()
    base = {"token": token, "publicKeyB64": _hub_pub_b64(),
            "aud": rc.DEFAULT_ATTACHMENT_AUDIENCE}
    assert _relay("verify_attachment", {**base, "relayId": OTHER_RELAY_ID,
                                        "realm": REALM})["grant"] is None
    assert _relay("verify_attachment", {**base, "relayId": RELAY_ID,
                                        "realm": OTHER_REALM})["grant"] is None


def test_attachment_expired_and_tampered_refused_by_real_relay():
    expired, _ = _mint_attachment(ttl_seconds=1, now=NOW - 600)
    grant = _relay("verify_attachment", {
        "token": expired, "publicKeyB64": _hub_pub_b64(),
        "relayId": RELAY_ID, "realm": REALM})["grant"]
    assert grant is None

    token, _ = _mint_attachment()
    parts = token.split(".")
    body = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    body["account_id"] = "attacker"
    forged = ".".join([parts[0], base64.urlsafe_b64encode(
        json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("="),
        parts[2]])
    grant = _relay("verify_attachment", {
        "token": forged, "publicKeyB64": _hub_pub_b64(),
        "relayId": RELAY_ID, "realm": REALM})["grant"]
    assert grant is None


def test_attachment_signed_by_wrong_key_refused_by_real_relay():
    other = Ed25519PrivateKey.generate()
    token, _ = _mint_attachment()
    grant = _relay("verify_attachment", {
        "token": token, "publicKeyB64": rc.attachment_public_key_b64(other),
        "relayId": RELAY_ID, "realm": REALM})["grant"]
    assert grant is None


def test_attachment_ttl_is_hard_capped_at_300_seconds():
    # The relay's verify enforces exp <= iat + 300; mint must clamp, because
    # a token that mints already-invalid is silence, not failure (jidoka).
    _token, claims = _mint_attachment(ttl_seconds=100_000)
    assert claims["exp"] - claims["iat"] == 300


def test_attachment_mint_validates_inputs():
    with pytest.raises(ValueError):
        _mint_attachment(daemon_id="bad id!")   # relay's daemon_id regex
    with pytest.raises(ValueError):
        _mint_attachment(daemon_key_b64=base64.b64encode(b"short").decode())
    with pytest.raises(ValueError):
        _mint_attachment(account_id="")
    with pytest.raises(ValueError):
        _mint_attachment(realm="")


# ---------------------------------------------------------------------------
# bdsr1 capabilities, verified by the REAL relay
# ---------------------------------------------------------------------------

RING = rc.CapKeyRing(
    [rc.CapKey(kid="k2026-08", secret="old-secret"),
     rc.CapKey(kid="k2026-09", secret="new-secret")],
    active_kid="k2026-09")


def test_capability_minted_in_python_verified_by_real_relay():
    token, payload = _mint_capability(RING, scopes=["terminal.proxy", "relay.inventory"])
    assert token.startswith("bdsr1.")
    assert payload["kid"] == "k2026-09"
    assert payload["scopes"] == ["relay.inventory", "terminal.proxy"]  # normalized
    assert payload["exp"] - payload["iat"] == 900

    result = _relay("verify_capability", {
        "token": token, "keys": [k.__dict__ for k in RING.verify_keys()],
        "targetId": "target-a", "relayId": RELAY_ID, "scope": "terminal.proxy",
        "typ": rc.DEFAULT_CAPABILITY_TYPE, "aud": rc.DEFAULT_CAPABILITY_AUDIENCE,
        "now": NOW + 10,
    })
    assert result["ok"] is True, result
    assert result["payload"]["account_id"] == "acct_livestack"


def test_capability_verifies_on_either_ring_key_during_overlap():
    # A token minted under the OLD key still verifies while the ring carries
    # both — the verify window rotation lives on (task 7.1's foundation).
    old_ring = rc.CapKeyRing(
        [rc.CapKey(kid="k2026-08", secret="old-secret")])
    token, payload = _mint_capability(old_ring)
    assert payload["kid"] == "k2026-08"
    result = _relay("verify_capability", {
        "token": token, "keys": [k.__dict__ for k in RING.verify_keys()],
        "targetId": "target-a", "relayId": RELAY_ID, "scope": "terminal.proxy",
        "typ": rc.DEFAULT_CAPABILITY_TYPE, "aud": rc.DEFAULT_CAPABILITY_AUDIENCE,
        "now": NOW + 10,
    })
    assert result["ok"] is True, result


def test_capability_refusals_from_the_real_relay():
    base = {"keys": [k.__dict__ for k in RING.verify_keys()],
            "targetId": "target-a", "relayId": RELAY_ID,
            "scope": "terminal.proxy",
            "typ": rc.DEFAULT_CAPABILITY_TYPE, "aud": rc.DEFAULT_CAPABILITY_AUDIENCE,
            "now": NOW + 10}

    # wrong scope
    token, _ = _mint_capability(RING)
    r = _relay("verify_capability", {**base, "token": token, "scope": "chipgen.generate"})
    assert r == {"ok": False, "reason": "scope_denied"}

    # wrong target
    r = _relay("verify_capability", {**base, "token": token, "targetId": "other-target"})
    assert r == {"ok": False, "reason": "target_mismatch"}

    # wrong relay
    r = _relay("verify_capability", {**base, "token": token, "relayId": OTHER_RELAY_ID})
    assert r == {"ok": False, "reason": "relay_mismatch"}

    # wrong realm cosmetics: a benchday-claims token is refused here
    benchday_token, _ = _mint_capability(
        RING, typ="benchday-speech-relay-capability", aud="benchday-speech-relay")
    r = _relay("verify_capability", {**base, "token": benchday_token})
    assert r == {"ok": False, "reason": "bad_capability_type"}

    # expired (relay allows a 30 s grace)
    dead, _ = _mint_capability(RING, ttl_seconds=60)
    r = _relay("verify_capability", {**base, "token": dead, "now": NOW + 120})
    assert r == {"ok": False, "reason": "capability_expired"}

    # tampered payload: swap the target, keep the signature
    parts = token.split(".")
    body = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    body["target_id"] = "other-target"
    forged = ".".join([parts[0], base64.urlsafe_b64encode(
        json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("="),
        parts[2]])
    r = _relay("verify_capability", {**base, "token": forged})
    assert r == {"ok": False, "reason": "bad_capability_signature"}

    # a kid the ring does not hold
    stranger = rc.CapKeyRing([rc.CapKey(kid="stranger", secret="nope")])
    foreign, _ = _mint_capability(stranger)
    r = _relay("verify_capability", {**base, "token": foreign})
    assert r == {"ok": False, "reason": "unknown_key_id"}


def test_capability_local_verify_mirror_matches_relay_reasons():
    # The local mirror (used by tests/diagnostics when node is absent) agrees
    # with the relay's refusal strings on the same negative cases.
    token, _ = _mint_capability(RING)
    ok, _payload, reason = rc.verify_capability(
        RING, token, target_id="other-target", relay_id=RELAY_ID,
        scope="terminal.proxy", now=NOW + 10)
    assert (ok, reason) == (False, "target_mismatch")
    ok, _payload, reason = rc.verify_capability(
        RING, token, target_id="target-a", relay_id=RELAY_ID,
        scope="chipgen.generate", now=NOW + 10)
    assert (ok, reason) == (False, "scope_denied")
    ok, payload, reason = rc.verify_capability(
        RING, token, target_id="target-a", relay_id=RELAY_ID,
        scope="terminal.proxy", now=NOW + 10)
    assert ok and payload["kid"] == "k2026-09"


# ---------------------------------------------------------------------------
# Key ring rotation (the drill's foundation — the drill itself is a later phase)
# ---------------------------------------------------------------------------

def test_ring_rotation_overlap_then_retire():
    ring_v1 = rc.CapKeyRing([rc.CapKey(kid="k1", secret="s1")])
    old_token, _ = _mint_capability(ring_v1)

    # Rotate: new key mints, old still verifies (the verify window).
    ring_v2 = ring_v1.rotate(rc.CapKey(kid="k2", secret="s2"))
    assert ring_v2.active.kid == "k2"
    assert set(ring_v2.kids) == {"k1", "k2"}
    new_token, payload = _mint_capability(ring_v2)
    assert payload["kid"] == "k2"
    ok, _p, _r = rc.verify_capability(ring_v2, old_token, target_id="target-a",
                                      scope="terminal.proxy", now=NOW + 10)
    assert ok, "token minted under k1 must still verify while k1 is in the ring"

    # Retire k1 only after its tokens are all dead (one TTL, <=15 min).
    ring_v3 = ring_v2.retire("k1")
    assert set(ring_v3.kids) == {"k2"}
    ok, _p, reason = rc.verify_capability(ring_v3, old_token, target_id="target-a",
                                          scope="terminal.proxy", now=NOW + 10)
    assert not ok and reason == "unknown_key_id"
    ok, _p, _r = rc.verify_capability(ring_v3, new_token, target_id="target-a",
                                      scope="terminal.proxy", now=NOW + 10)
    assert ok


def test_ring_refuses_ambiguous_operations():
    ring = rc.CapKeyRing([rc.CapKey(kid="k1", secret="s1")])
    with pytest.raises(ValueError):
        ring.rotate(rc.CapKey(kid="k1", secret="different"))  # kid shadowing
    with pytest.raises(ValueError):
        ring.retire("k1")          # cannot retire the minting key
    with pytest.raises(ValueError):
        ring.retire("nope")        # not in the ring
    with pytest.raises(ValueError):
        rc.CapKeyRing([])          # empty ring
    with pytest.raises(ValueError):
        rc.CapKeyRing([rc.CapKey(kid="k1", secret="a"),
                       rc.CapKey(kid="k1", secret="b")])  # duplicate kid


def test_ring_from_json_matches_relay_env_shape():
    # Same JSON the relay's relayKeyRingFromEnv parses.
    raw = json.dumps([
        {"kid": "k-old", "secret": "s-old"},
        {"kid": "k-new", "secret": "s-new", "active": True},
        {"kid": "", "secret": "skipped"},
    ])
    ring = rc.CapKeyRing.from_json(raw)
    assert ring.active.kid == "k-new"
    assert set(ring.kids) == {"k-old", "k-new"}
    with pytest.raises(ValueError):
        rc.CapKeyRing.from_json(json.dumps({}))   # not an array


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------

def _env(**overrides):
    env = {
        "LIVESTACK_RELAY_URLS": "https://relay-a.example, https://relay-b.example/",
        "LIVESTACK_RELAY_REALM": "livestack",
        "LIVESTACK_RELAY_CAP_KEYS": json.dumps(
            [{"kid": "k1", "secret": "s1", "active": True}]),
        "LIVESTACK_RELAY_QUOTA_MAX_STREAMS": "64",
        "LIVESTACK_RELAY_QUOTA_MAX_REQ_PER_MIN": "4000",
    }
    env.update(overrides)
    return env


def test_config_from_env():
    cfg = rc.RelayConfig.from_env(_env(LIVESTACK_RELAY_KEY=rc_key_pem()))
    assert cfg.urls == ("https://relay-a.example", "https://relay-b.example/")
    assert cfg.realm == "livestack"
    assert cfg.configured is True
    assert cfg.attachment_key is not None
    assert cfg.cap_ring is not None and cfg.cap_ring.active.kid == "k1"
    assert cfg.quota.max_streams_per_account == 64
    assert cfg.quota.max_requests_per_min == 4000
    assert cfg.quota.max_stream_seconds is None

    token, claims = cfg.mint_attachment_for(
        daemon_id="gpu-box-7", daemon_key_b64=_daemon_key_b64(),
        relay_id=RELAY_ID, account_id="acct", now=NOW)
    assert claims["realm"] == "livestack" and claims["aud"] == cfg.attachment_audience
    cap, payload = cfg.mint_capability_for(
        account_id="acct", device_id="d", target_id="t", relay_id=RELAY_ID,
        region="local", scopes=["terminal.proxy"], now=NOW)
    assert payload["typ"] == cfg.capability_type


def test_config_unset_means_do_not_attach():
    cfg = rc.RelayConfig.from_env({})
    assert cfg.configured is False
    assert cfg.urls == ()
    assert cfg.attachment_key is None


def test_config_key_file_wins_and_enforces_0600(tmp_path):
    key_file = tmp_path / "relay-key.pem"
    key_file.write_text(rc_key_pem())
    key_file.chmod(0o600)
    cfg = rc.RelayConfig.from_env(_env(LIVESTACK_RELAY_KEY_FILE=str(key_file),
                                       LIVESTACK_RELAY_KEY="garbage"))
    assert cfg.attachment_key is not None  # file beats inline

    key_file.chmod(0o644)
    logs = []
    with pytest.raises(ValueError):
        rc.RelayConfig.from_env(_env(LIVESTACK_RELAY_KEY_FILE=str(key_file)),
                                log=logs.append)
    assert any("0600" in line for line in logs), logs

    key_file.chmod(0o600)
    key_file.unlink()
    logs = []
    with pytest.raises(ValueError):
        rc.RelayConfig.from_env(_env(LIVESTACK_RELAY_KEY_FILE=str(key_file)),
                                log=logs.append)
    assert any("cannot be read" in line for line in logs), logs


def test_quota_env_rejects_garbage():
    with pytest.raises(ValueError):
        rc.RelayQuotaConfig.from_env({"LIVESTACK_RELAY_QUOTA_MAX_STREAMS": "lots"})
    with pytest.raises(ValueError):
        rc.RelayQuotaConfig.from_env({"LIVESTACK_RELAY_QUOTA_MAX_STREAMS": "0"})


_RC_PEM = None


def rc_key_pem() -> str:
    global _RC_PEM
    if _RC_PEM is None:
        _RC_PEM = ATTACH_KEY_PEM.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
    return _RC_PEM
