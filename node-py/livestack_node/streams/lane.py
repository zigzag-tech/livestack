"""Standalone driver for benchday's isolated lane: the producers against a REAL daemon, no Harmony needed.

Copy this directory (`livestack_node/streams/`) into the container under any package name, e.g.

    cp -r node-py/livestack_node/streams /work/hstreams
    PYTHONPATH=/work python3 -m hstreams.lane --socket "$XDG_STATE_HOME/benchday-daemon/streams-ingress.sock" \\
        --host lane-host --dir /tmp/hstreams-state --units-glob '/tmp/units/*units*.json' \\
        --fleet-json fleet.json --status-json status.json --units-json units.json --seconds 30

It needs only python3 (3.9+) and the standard library. It registers/serves the contracts for which input
was given, drives them with scripted sources, and prints one JSON object per line on stdout:
`{"event": "up"|"log"|"done", ...}`. Exit status 0 when it ran its time, 2 on a usage error.

    --units-glob     enables harmony.host/1 (digest of the matching files; edit a file to see it change)
    --fleet-json     enables harmony.fleet/1 from a /fleet document (with --status-json for /status)
    --units-json     enables harmony.request/1: a JSON list of {name, host, attributes, resident, footprint}
    --workload       enables harmony.workload/1 over `ScriptedBackend`: the intent's `kind` picks the
                     outcome: `ok` runs to done; `fail-product` ends failed/workload; `fail-infra` ends
                     failed/infra (lease expired); `refuse-429` is refused by the job authority;
                     anything else stays admitted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .common import wall_ms
from .host_facts import HostFacts, UnitDeclarations
from .request_authority import RequestAuthority, request_ledger
from .runtime import StreamsRuntime
from .workload_authority import BackendRefused, IntentLedger, WorkloadAuthority


class ScriptedBackend:
    """An in-memory job authority whose jobs advance one rung per `poll`, shaped like WorkloadStore._job."""

    def __init__(self) -> None:
        self.jobs: Dict[str, dict] = {}
        self.clock = lambda: wall_ms() / 1000.0

    def submit(self, owner: str, kind: str, spec: Optional[dict], selector: dict, intent_id: str) -> dict:
        if kind == "refuse-429":
            raise BackendRefused(429, "job storage capacity exhausted")
        job = self.jobs.setdefault(intent_id, {
            "id": "job-" + intent_id, "owner": owner, "state": "queued", "created": self.clock(), "updated": self.clock(),
            "spec": {"handler": kind, "selector": selector}, "attempts": [], "result": None, "reason": None, "_n": 0})
        return job

    def poll(self, job_id: str) -> Optional[dict]:
        job = next((j for j in self.jobs.values() if j["id"] == job_id), None)
        if job is None or job["state"] in ("succeeded", "failed", "cancelled", "expired"):
            return job
        job["_n"] += 1
        kind = job["spec"]["handler"]
        if kind == "stay" or job["_n"] < 1:
            return job
        attempt = {"id": "att-1", "worker": "w1", "host": "h1", "state": "running", "expires": self.clock() + 120}
        if job["_n"] == 1:
            job["state"], job["attempts"] = "running", [attempt]
        elif job["_n"] == 2:
            job["progress"] = {"pct": 10}
        else:
            job["updated"] = self.clock()
            if kind == "ok":
                job["state"] = "succeeded"
            elif kind == "fail-product":
                job["state"], job["result"] = "failed", {"outcome": "product_failure", "result": {"error": "tests_red", "detail": "3 failed"}}
            elif kind == "fail-infra":
                job["state"], job["reason"] = "failed", "execution lease expired"
                job["result"] = {"outcome": "infrastructure", "result": {"error": "abandoned", "detail": "execution lease expired"}}
                job["attempts"] = [dict(attempt, state="cleanup")]
        return job

    def poll_many(self, job_ids: List[str]) -> Dict[str, Optional[dict]]:
        return {job_id: self.poll(job_id) for job_id in job_ids}

    def cancel(self, owner: str, job_id: str) -> None:
        for job in self.jobs.values():
            if job["id"] == job_id:
                job["state"], job["reason"] = "cancelled", "cancelled by owner"


def _load(path: Optional[str]) -> Any:
    if not path:
        return None
    with open(path, "rb") as handle:
        return json.load(handle)


async def run(args: argparse.Namespace) -> None:
    def emit(**row: Any) -> None:
        sys.stdout.write(json.dumps(row, sort_keys=True) + "\n")
        sys.stdout.flush()

    log = lambda message: emit(event="log", message=message)  # noqa: E731
    runtime = StreamsRuntime(args.socket, log, args.host, tick_s=args.tick, fleet_s=args.tick)
    facts = (HostFacts(args.host, UnitDeclarations(tuple(args.units_glob)), os.path.join(args.dir, "host_facts.json"),
                       runtime.host_publisher(), heartbeat_s=args.heartbeat, log=log) if args.units_glob else None)
    fleet, status = _load(args.fleet_json), _load(args.status_json) or {}
    units = _load(args.units_json)
    workload = None
    if args.workload:
        workload = WorkloadAuthority(IntentLedger(os.path.join(args.dir, "intents.sqlite")), ScriptedBackend(),
                                     runtime.intent_publisher("harmony.workload/1"), f"harmony-workload:{args.host}", log=log)
    request = None
    if units is not None:
        request = RequestAuthority(request_ledger(os.path.join(args.dir, "requests.sqlite")), args.host,
                                   lambda scope, host: [u for u in units if scope == "fleet" or u.get("host") in (None, host)],
                                   runtime.intent_publisher("harmony.request/1"), f"harmony-request:{args.host}", log=log)
    runtime.attach(host_facts=facts, fleet_reader=(lambda: (fleet, status)) if fleet is not None else None,
                   workload=workload, request=request)
    runtime.start()
    emit(event="up", host=args.host, contracts=sorted(runtime.contracts()[0] + list(runtime.contracts()[1])))
    await asyncio.sleep(args.seconds)
    await runtime.stop()
    emit(event="done", fleet_published=runtime.fleet_published, modes=runtime.modes)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="lane", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--dir", required=True, help="state directory for the bounded state files")
    parser.add_argument("--units-glob", action="append", default=[])
    parser.add_argument("--fleet-json")
    parser.add_argument("--status-json")
    parser.add_argument("--units-json")
    parser.add_argument("--workload", action="store_true")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--tick", type=float, default=1.0)
    parser.add_argument("--heartbeat", type=float, default=30.0)
    args = parser.parse_args(argv)
    if not (args.units_glob or args.fleet_json or args.units_json or args.workload):
        parser.error("nothing to run: give --units-glob, --fleet-json, --units-json or --workload")
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
