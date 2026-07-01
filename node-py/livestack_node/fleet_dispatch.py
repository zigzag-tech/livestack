"""fleet_dispatch.py — execute a :class:`FleetPlan` against real providers.

:func:`fleet_scheduler.schedule` is a pure function that decides **which** target
each job runs on; this is the impure other half the design names ("dispatch the
actions — provision via the adapters, admit via the job client"). It maps the
plan's :class:`Provision` actions onto the provisioning layer
(:class:`provision.Provisioner`) and runs the caller's workload on the leased box.
RunPod is one provider today; Aliyun ECI slots in as another entry in the
``registry`` with **no change to this file** — provider fall-through then falls out
of the scheduler's tier ordering plus :class:`provision.CapacityError`.

Kept separate from ``schedule`` so the scheduler stays pure/testable and the caller
(the broker, or a script like Benchday's retrain) owns all I/O.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

from .fleet_scheduler import Admit, Deprovision, FleetPlan, Provision, Queue, Target
from .provision import CapacityError, ComputeHandle, ComputeSpec, Provisioner, leased

RunJob = Callable[[str, ComputeHandle], None]     # (job_id, handle) -> run the workload
SpecFor = Callable[[str], ComputeSpec]            # job_id -> the ComputeSpec to acquire


def run_on_provider(provisioner: Provisioner, spec: ComputeSpec,
                    run: Callable[[ComputeHandle], None], *, max_tries: int = 5):
    """Lease a box from ``provisioner`` and run ``run(handle)`` on it, trying its
    offers cheapest-first and falling through on capacity miss / workload failure
    (each attempt's box is ALWAYS torn down by :func:`leased`). Returns the winning
    :class:`Offer`; raises :class:`CapacityError` once every offer is exhausted so a
    caller can fall through to another provider."""
    offers = provisioner.offers(spec)[:max_tries]
    if not offers:
        raise CapacityError(f"{provisioner.provider}: no eligible offers")
    last: Optional[Exception] = None
    for offer in offers:
        try:
            with leased(provisioner, spec, offer) as handle:
                run(handle)
            return offer
        except Exception as e:  # noqa: BLE001 — CapacityError or workload error; box torn down
            last = e
            print(f"[dispatch] {offer}: {e}; next candidate")
    raise CapacityError(f"{provisioner.provider}: all {len(offers)} offers exhausted; last: {last}")


def dispatch(plan: FleetPlan, targets: Dict[str, Target],
             registry: Dict[str, Provisioner], *, run_job: RunJob,
             spec_for: SpecFor, max_tries: int = 5) -> Dict[str, str]:
    """Execute ``plan``. ``targets`` maps target_id -> Target (to resolve a
    Provision's provider via ``target.labels['provider']``); ``registry`` maps a
    provider name -> its Provisioner. Returns ``{job_id: status}`` where status is
    ``done`` / ``failed`` / ``queued`` / ``unroutable``.

    Provision  -> lease on the target's provider and run the job.
    Admit      -> run on an existing running worker; unused by cloud-burst training
                  (no persistent trainer registered), so reported ``unroutable``.
    Queue      -> the scheduler deferred it (no feasible target now).
    Deprovision-> ephemeral pods self-drain after each job, so a no-op here.
    """
    results: Dict[str, str] = {}
    for a in plan.actions:
        if isinstance(a, Provision):
            target = targets.get(a.target_id)
            provider = target.labels.get("provider") if target else None
            provisioner = registry.get(provider) if provider else None
            if provisioner is None:
                print(f"[dispatch] no provisioner for target {a.target_id} (provider={provider!r})")
                results[a.job_id] = "unroutable"
                continue
            print(f"[dispatch] provision {a.job_id} on {provider} [{a.tier.name}] (est ${a.est_cost:.2f})")
            try:
                offer = run_on_provider(provisioner, spec_for(a.job_id),
                                        lambda h, j=a.job_id: run_job(j, h), max_tries=max_tries)
                print(f"[dispatch] {a.job_id} done on {offer}")
                results[a.job_id] = "done"
            except Exception as e:  # noqa: BLE001 — all offers exhausted / unrecoverable
                print(f"[dispatch] {a.job_id} FAILED: {e}")
                results[a.job_id] = "failed"
        elif isinstance(a, Admit):
            print(f"[dispatch] Admit {a.job_id}->{a.target_id}: no running-worker handler registered")
            results[a.job_id] = "unroutable"
        elif isinstance(a, Queue):
            print(f"[dispatch] queued {a.job_id}: {a.reason}")
            results[a.job_id] = "queued"
        elif isinstance(a, Deprovision):
            print(f"[dispatch] deprovision {a.target_id}: {a.reason} (ephemeral pods self-drain; noop)")
    return results
