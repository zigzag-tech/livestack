"""The policy artifact routes (scheduler-policy-routine task 3.5, design §6):
spec requirement "Only a validated, authorised artifact changes routing"."""
import json
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from livestack_node.fleet_auth import load_principals  # noqa: E402
from livestack_node.hostbroker import HostBroker  # noqa: E402
from livestack_node.hostd import build_app  # noqa: E402
from livestack_node.policy_runtime import POLICY_ID, PolicyRuntime  # noqa: E402
from policy_fakes import FakeNative, FakeRecorder, artifact  # noqa: E402

TOK_ADMIN = "p" * 40
TOK_PLAIN = "q" * 40
PRINCIPALS = load_principals(json.dumps({
    TOK_ADMIN: {"name": "policy-improver", "owner": "livestack:policy",
                "capabilities": ["policy_admin", "bogus"]},
    TOK_PLAIN: {"name": "attune", "owner": "attune"}}))
ADMIN = {"Authorization": f"Bearer {TOK_ADMIN}"}
URL = f"/fleet/policy/{POLICY_ID}"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _client(tmp_path, *, principals=PRINCIPALS, native=True, calls=None):
    fake = FakeNative()
    validator = None
    if native:
        def validator(text):
            if calls is not None:
                calls.append(text)
            return fake.load(text)[1:]
    b = HostBroker()
    b.fleet_view = lambda: {"generated_at": 0.0, "hosts": {}}
    b.fleet_principals = principals
    clock = Clock()
    # The runtime itself has no native module: only the publish gate does.
    b.policy_runtime = PolicyRuntime(str(tmp_path), native=None, recorder=FakeRecorder(),
                                     validator=validator, clock=clock)
    return TestClient(build_app(b), raise_server_exceptions=False), b, clock


def _active(tmp_path):
    p = tmp_path / f"{POLICY_ID}.active.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_fleet_auth_off_is_403_and_routing_is_unchanged(tmp_path):
    client, b, _ = _client(tmp_path, principals=None)
    r = client.put(URL, json=artifact(w_distance=0.0))
    assert r.status_code == 403 and "requires fleet auth" in r.json()["detail"]
    assert _active(tmp_path) is None and b.policy_runtime.status()["source"] == "defaults"
    assert client.post(f"{URL}/revert").status_code == 403


def test_unknown_token_is_401_and_a_principal_without_the_capability_is_403(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.put(URL, json=artifact(),
                      headers={"Authorization": "Bearer " + "z" * 40}).status_code == 401
    r = client.put(URL, json=artifact(), headers={"Authorization": f"Bearer {TOK_PLAIN}"})
    assert r.status_code == 403 and "policy_admin" in r.json()["detail"]
    assert _active(tmp_path) is None


def test_native_unavailable_is_503_and_nothing_is_written(tmp_path):
    client, _, _ = _client(tmp_path, native=False)
    r = client.put(URL, json=artifact(w_distance=0.0), headers=ADMIN)
    assert r.status_code == 503
    assert "without livestack_policy" in r.json()["detail"]
    assert _active(tmp_path) is None


def test_out_of_bounds_param_is_422_listing_every_violation(tmp_path):
    client, _, _ = _client(tmp_path)
    bad = artifact()
    bad["params"]["w_budget"] = 11
    bad["params"]["w_speed"] = -1
    bad["params"]["surprise"] = 1.0
    r = client.put(URL, json=bad, headers=ADMIN)
    assert r.status_code == 422
    codes = [v["code"] for v in r.json()["detail"]["violations"]]
    assert codes.count("param_out_of_bounds") == 2 and "param_unknown" in codes
    assert _active(tmp_path) is None


def test_a_valid_artifact_is_written_atomically_and_the_previous_kept(tmp_path):
    client, b, _ = _client(tmp_path)
    a, c = artifact(w_distance=0.0), artifact(w_distance=1.0)
    r = client.put(URL, json=a, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json() == {"policy_id": POLICY_ID, "role": "active",
                        "version": a["version"], "previous_version": None}
    r = client.put(URL, json=c, headers=ADMIN)
    assert r.json()["previous_version"] == a["version"]
    assert _active(tmp_path)["version"] == c["version"]
    assert json.loads((tmp_path / f"{POLICY_ID}.previous.json").read_text())["version"] \
        == a["version"]
    assert not [f for f in os.listdir(tmp_path) if ".tmp." in f]
    g = client.get(URL).json()
    assert g["active"]["version"] == c["version"] and g["previous"]["version"] == a["version"]
    assert g["source"] == "file"


def test_revert_restores_the_previous_without_a_model(tmp_path):
    calls = []
    client, b, clock = _client(tmp_path, calls=calls)
    a, c = artifact(w_distance=0.0), artifact(w_distance=1.0)
    client.put(URL, json=a, headers=ADMIN)
    client.put(URL, json=c, headers=ADMIN)
    validations = len(calls)
    r = client.post(f"{URL}/revert", headers=ADMIN)
    assert r.status_code == 200, r.text
    # active within one reload interval (here: immediately)
    assert b.policy_runtime.artifact_version == a["version"]
    assert r.json()["version"] == a["version"] and r.json()["previous_version"] == c["version"]
    assert len(calls) == validations          # a file swap: nothing was asked of anyone
    # and the revert can itself be reverted
    client.post(f"{URL}/revert", headers=ADMIN)
    assert b.policy_runtime.artifact_version == c["version"]


def test_revert_with_no_previous_is_409(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.post(f"{URL}/revert", headers=ADMIN).status_code == 409


def test_a_file_that_fails_validation_keeps_the_previous_artifact(tmp_path):
    client, b, clock = _client(tmp_path)
    good = artifact(w_distance=0.0)
    client.put(URL, json=good, headers=ADMIN)
    # someone hand-edits the active file into something invalid
    bad = dict(good, params=dict(good["params"], w_budget=99))
    (tmp_path / f"{POLICY_ID}.active.json").write_text(json.dumps(bad))
    st = os.stat(tmp_path / f"{POLICY_ID}.active.json")
    os.utime(tmp_path / f"{POLICY_ID}.active.json",
             ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
    clock.t += 6
    b.policy_runtime.reload_if_changed()      # what the next decision does
    g = client.get(URL).json()
    assert g["active"]["version"] == good["version"]
    assert "w_budget" in g["last_load_error"]
    fleet = client.get("/fleet").json()
    assert "policy_artifact_invalid" in fleet["degraded"]
    assert fleet["policy"]["active"]["version"] == good["version"]


def test_shadow_publish_takes_at_most_two(tmp_path):
    client, b, _ = _client(tmp_path)
    two = [artifact(w_distance=0.0), artifact(w_distance=1.0)]
    r = client.put(URL + "?role=shadow", json=two, headers=ADMIN)
    assert r.status_code == 200 and r.json()["version"] == [x["version"] for x in two]
    assert [s["version"] for s in b.policy_runtime.status()["shadow"]] == r.json()["version"]
    r = client.put(URL + "?role=shadow", json=two + [artifact()], headers=ADMIN)
    assert r.status_code == 422
    # shadows never become active
    assert b.policy_runtime.status()["source"] == "defaults"


def test_a_wrong_policy_id_is_404_and_a_foreign_artifact_422(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.put("/fleet/policy/other.policy", json=artifact(),
                      headers=ADMIN).status_code == 404
    foreign = artifact()
    foreign["policy_id"] = "other.policy"
    r = client.put(URL, json=foreign, headers=ADMIN)
    assert r.status_code == 422
    assert "policy_id_mismatch" in [v["code"] for v in r.json()["detail"]["violations"]]


def test_get_fleet_reports_policy_status_and_degraded(tmp_path):
    client, _, _ = _client(tmp_path)
    fleet = client.get("/fleet").json()
    assert fleet["policy"]["policy_id"] == POLICY_ID
    assert "policy_artifact_missing" in fleet["degraded"]
    assert "policy_native_unavailable" in fleet["degraded"]


def test_unknown_capabilities_are_dropped():
    admin = PRINCIPALS[TOK_ADMIN]
    assert admin.capabilities == frozenset({"policy_admin"})
    assert not PRINCIPALS[TOK_PLAIN].can("policy_admin")
