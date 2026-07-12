"""provision.py — general ephemeral-compute provisioning for burst workloads.

A :class:`Provisioner` leases a short-lived, SSH-reachable compute instance (a
rented GPU box) and **guarantees teardown**; the workload that runs on it is
provider-agnostic (rsync in, remote-exec, rsync out). RunPod, Aliyun ECI,
Lambda, Vast, … are each *one* Provisioner; a training / eval / batch job is
*one* workload that runs on any of them.

Where this sits in Harmony: :mod:`fleet_scheduler` decides **which** target to
run on (rank by cost / speed / locality, burst to cloud when local is busy); a
Provisioner does **how** — ``acquire`` an instance for a chosen ``Offer`` and
``release`` it. :func:`leased` supplies the one invariant that makes renting
money-safe: the instance is *always* torn down — on success, on workload error,
and on SIGTERM/SIGINT — and :func:`reap_orphans` is the crash backstop for the
one window no in-process handler survives (SIGKILL / power loss).

The module is intentionally **zero-dependency (stdlib only)** so it imports
anywhere — including the macOS system python that drives Benchday's weekly
retrain — with no coupling to the rest of livestack.

Fallback across providers is expressed with :class:`CapacityError`: a caller
walks the scheduler-ranked offers and treats ``CapacityError`` (this offer is
out of stock) as "try the next one".
"""
from __future__ import annotations

import abc
import contextlib
import signal
import subprocess
from dataclasses import dataclass, field
from typing import List, Mapping, Optional


# --- errors -----------------------------------------------------------------
class ProvisionError(Exception):
    """An instance could not be provisioned or prepared (created but never became
    usable, teardown-worthy). The caller should move to the next offer."""


class CapacityError(ProvisionError):
    """The requested offer is not obtainable right now (out of stock / quota).
    Nothing was created, so nothing needs teardown. Drives provider fall-through:
    try ECI, on ``CapacityError`` fall through to RunPod, etc."""


# --- value types ------------------------------------------------------------
@dataclass(frozen=True)
class ComputeSpec:
    """What the workload needs from a box. Providers translate this into their own
    catalog (a RunPod gpuType, an ECI instance family, …)."""
    name: str = "livestack"                    # instance name; also the reap prefix root
    min_vram_gb: int = 24
    max_vram_gb: int = 96
    gpu_hint: Optional[str] = None             # optional preferred SKU substring
    image: Optional[str] = None                # container image / AMI (provider default if None)
    env: Mapping[str, str] = field(default_factory=dict)   # extra instance env


@dataclass(frozen=True)
class Offer:
    """A concrete, priced way to satisfy a spec on one provider (a specific SKU in
    a specific zone). :meth:`Provisioner.offers` returns these ranked cheapest-first;
    the caller (or the fleet scheduler) picks one and hands it back to ``acquire``."""
    provider: str
    sku: str                                   # provider SKU id (e.g. "NVIDIA RTX A5000")
    zone: str = ""                             # cloud/region ("COMMUNITY", "cn-hangzhou", …)
    price_per_hr: float = 0.0
    meta: Mapping[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        tail = self.sku.split()[-1] if self.sku else "?"
        return f"{self.provider}:{tail}/{self.zone[:3]}=${self.price_per_hr}"


@dataclass(frozen=True)
class Instance:
    """A live instance as seen by :meth:`Provisioner.list_instances` — enough to
    decide whether :func:`reap_orphans` should kill it."""
    id: str
    name: str = ""
    age_min: float = 0.0
    price_per_hr: Optional[float] = None


# --- remote exec surface (SSH today; the seam for a future transport) --------
_SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20"]
_KEEPALIVE = ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]


def _ssh(host: str, port: int, user: str, cmd: str, *, check: bool = True,
         timeout: int = 600) -> subprocess.CompletedProcess:
    # keepalive is load-bearing for long exec: an idle channel (output buffered
    # behind `| tail`) is dropped by NAT/proxy idle timeouts mid-run.
    return subprocess.run(["ssh", *_SSH_OPTS, *_KEEPALIVE, "-p", str(port),
                           f"{user}@{host}", cmd], check=check, timeout=timeout)


def _scp_up(host: str, port: int, user: str, src: str, dst: str, *,
            recursive: bool = False, timeout: int = 600) -> None:
    args = ["scp", *_SSH_OPTS, *_KEEPALIVE, "-P", str(port)]
    if recursive:
        args.append("-r")
    args += [src, f"{user}@{host}:{dst}"]
    subprocess.run(args, check=True, timeout=timeout)


def _scp_down(host: str, port: int, user: str, src: str, dst: str, *,
              timeout: int = 600) -> None:
    subprocess.run(["scp", *_SSH_OPTS, *_KEEPALIVE, "-P", str(port), "-r",
                    f"{user}@{host}:{src}", dst], check=True, timeout=timeout)


@dataclass
class ComputeHandle:
    """A leased instance's exec surface. Providers construct this from ``acquire``;
    workloads use ``push`` / ``exec`` / ``pull`` and never touch the provider API.

    The transport is SSH today. The three methods are the deliberate seam: a
    non-SSH provider (a serverless endpoint, a k8s exec) can implement the same
    surface differently without changing any workload — but we do NOT abstract
    the transport speculatively until such a provider actually exists.
    """
    provider: str
    instance_id: str
    host: str
    port: int
    user: str = "root"
    offer: Optional[Offer] = None

    def exec(self, cmd: str, *, check: bool = True, timeout: int = 600):
        return _ssh(self.host, self.port, self.user, cmd, check=check, timeout=timeout)

    def push(self, src: str, dst: str, *, recursive: bool = False, timeout: int = 600) -> None:
        _scp_up(self.host, self.port, self.user, src, dst, recursive=recursive, timeout=timeout)

    def pull(self, src: str, dst: str, *, timeout: int = 600) -> None:
        _scp_down(self.host, self.port, self.user, src, dst, timeout=timeout)


# --- the provider contract --------------------------------------------------
class Provisioner(abc.ABC):
    """Lease ephemeral compute from one provider. Subclasses implement four
    primitives; the base supplies the money-safety discipline (``_live`` tracking
    + idempotent ``release``) so a provider author cannot forget teardown.

    Contract:
      * ``offers``  — ranked, cheapest-first ways to satisfy the spec (may be []).
      * ``acquire`` — create ONE instance for ``offer`` and return a reachable
        handle. Register the instance in ``self._live`` the instant it is created
        (before it is reachable), so a signal mid-provision still tears it down.
        Raise :class:`CapacityError` if the offer is out of stock (nothing
        created); raise :class:`ProvisionError` if it was created but never became
        usable — and tear that instance down before raising.
      * ``_terminate`` — idempotent low-level delete of one instance id.
      * ``list_instances`` — live instances whose name starts with a prefix
        (for :func:`reap_orphans`).
    """
    provider: str = "base"

    def __init__(self) -> None:
        self._live: set[str] = set()

    @abc.abstractmethod
    def offers(self, spec: ComputeSpec) -> List[Offer]: ...

    @abc.abstractmethod
    def acquire(self, spec: ComputeSpec, offer: Optional[Offer] = None) -> ComputeHandle: ...

    @abc.abstractmethod
    def _terminate(self, instance_id: str) -> None: ...

    @abc.abstractmethod
    def list_instances(self, name_prefix: str) -> List[Instance]: ...

    # -- teardown (shared) ---------------------------------------------------
    def release(self, handle: ComputeHandle) -> None:
        self.release_id(handle.instance_id)

    def release_id(self, instance_id: str) -> None:
        """Idempotent teardown of one instance; used by ``release`` and by the
        signal handler. Always drops the id from ``_live`` even if terminate raised."""
        try:
            self._terminate(instance_id)
        finally:
            self._live.discard(instance_id)


# --- money-safety: guaranteed teardown, incl. on signal ---------------------
_ACTIVE: List[Provisioner] = []
_handlers_installed = False


def _teardown_all(signum, _frame):
    for p in _ACTIVE:
        for iid in list(p._live):
            try:
                p.release_id(iid)
            except Exception as e:  # noqa: BLE001 — best-effort; never mask the exit
                print(f"[provision] signal teardown of {iid} failed: {e}")
    raise SystemExit(f"[provision] caught signal {signum}; leased instances torn down")


def _install_signal_teardown() -> None:
    global _handlers_installed
    if _handlers_installed:
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _teardown_all)
        except (ValueError, OSError):
            pass  # not on the main thread — leased()'s finally still covers normal exit
    _handlers_installed = True


def _register(provisioner: Provisioner) -> None:
    if provisioner not in _ACTIVE:
        _ACTIVE.append(provisioner)
    _install_signal_teardown()


@contextlib.contextmanager
def leased(provisioner: Provisioner, spec: ComputeSpec, offer: Optional[Offer] = None):
    """Acquire an instance, yield its handle, and ALWAYS release it — on normal
    exit, on any workload exception, and on SIGTERM/SIGINT. If ``acquire`` fails it
    has already cleaned up after itself (or created nothing), so nothing leaks.

        with leased(RunpodProvisioner(), spec, offer) as box:
            box.push("job/", "/root/job")
            box.exec("python -m job")
            box.pull("/root/out", "./out")
    """
    _register(provisioner)
    handle = provisioner.acquire(spec, offer)   # raises before yield => no handle to release
    try:
        yield handle
    finally:
        provisioner.release(handle)


def reap_orphans(provisioner: Provisioner, name_prefix: str,
                 older_than_min: float = 120.0) -> List[str]:
    """Crash backstop: terminate any of ``provisioner``'s instances named
    ``name_prefix*`` older than ``older_than_min``. No real burst job lasts that
    long, so a concurrent fresh run is never touched. Best-effort — returns the
    ids reaped. Call once at startup before acquiring."""
    reaped: List[str] = []
    try:
        for inst in provisioner.list_instances(name_prefix):
            if inst.name.startswith(name_prefix) and inst.age_min > older_than_min:
                print(f"[provision] reaping orphan {inst.id} "
                      f"({inst.name}, {inst.age_min:.0f}min)")
                provisioner._terminate(inst.id)
                reaped.append(inst.id)
    except Exception as e:  # noqa: BLE001
        print(f"[provision] reap_orphans skipped: {e}")
    return reaped
