"""Fakes for the native `livestack_policy` module and its Recorder
(scheduler-policy-routine groups 2-3 run before the native crate exists).

`FakeNative` has the surface `policy_runtime.NativePolicy` exposes: `load`,
`decide`, `version_of`, `recorder`. Its versions are NOT the crate's blake3
versions; they only have to be stable and change when the params change."""
import hashlib
import json

from livestack_node.policy_runtime import (
    DEFAULT_PARAMS, choose_target_reference, defaults_artifact, greedy_decision,
    python_violations,
)


def fake_version(art: dict) -> str:
    blob = json.dumps({k: art.get(k) for k in ("exploration", "family", "params")},
                      sort_keys=True, separators=(",", ":"))
    return "b3:fake" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def artifact(exploration=None, **params) -> dict:
    """A valid artifact: the defaults with `params` overridden."""
    art = defaults_artifact()
    art["params"] = {**DEFAULT_PARAMS, **params}
    if exploration is not None:
        art["exploration"] = exploration
    art["version"] = fake_version(art)
    return art


class FakeHandle:
    def __init__(self, art):
        self.art = art
        self.version = art["version"]


class FakeNative:
    """Decides with the reference (so it agrees) unless `disagree` is set, in
    which case it reports every eligible row's score shifted and the greedy
    choice moved to the last eligible candidate."""

    def __init__(self, disagree=False):
        self.disagree = disagree
        self.decisions = 0

    def load(self, text):
        try:
            art = json.loads(text)
        except ValueError as e:
            return None, None, [{"code": "not_an_artifact", "detail": str(e)}]
        v = python_violations(art)
        if not v and art.get("version") != fake_version(art):
            v = [{"code": "version_mismatch", "detail": art.get("version")}]
        if v:
            return None, None, v
        return FakeHandle(art), art["version"], []

    def version_of(self, art):
        return fake_version(art)

    def decide(self, handle, ctx, candidates, decision_id):
        self.decisions += 1
        params = {k: float(x) for k, x in handle.art["params"].items()}
        rows = choose_target_reference(params, ctx, candidates)
        d = greedy_decision(rows, decision_id=decision_id,
                            artifact_version=handle.version, ctx=ctx,
                            candidates=candidates)
        if self.disagree:
            rows = [dict(r, score=r["score"] + 1.0) if r["eligible"] else r for r in rows]
            elig = [r["id"] for r in rows if r["eligible"]]
            g = elig[-1] if elig else None
            d = {**d, "rows": rows, "greedy": g, "chosen": g}
        return d

    def recorder(self, **cfg):
        return FakeRecorder(**cfg)


class FakeRecorder:
    """`record(dict) -> bool` (False = dropped), `stats() -> dict`, `close()`."""

    def __init__(self, capacity=None, **cfg):
        self.cfg = cfg
        self.lines = []
        self.capacity = capacity
        self.dropped = 0
        self.last_error = None
        self.closed = False

    def record(self, line):
        if self.capacity is not None and len(self.lines) >= self.capacity:
            self.dropped += 1
            return False
        self.lines.append(json.loads(json.dumps(line)))
        return True

    def stats(self):
        return {"written": len(self.lines), "dropped": self.dropped, "queued": 0,
                "rotations": 0, "last_error": self.last_error, "last_flush_ts": None}

    def close(self):
        self.closed = True

    def of(self, kind):
        return [l for l in self.lines if l["record"] == kind]
