"""fleet_workers.py — creating a REUSABLE fleet worker, and knowing whether you did.

:mod:`provision` rents an **ephemeral** box, runs one workload on it and
guarantees teardown. That is the right shape for a training run and the wrong
shape for fleet burst: a burst worker comes up, announces itself to the broker,
and then serves whatever the fleet admits to it until it is drained. Its
lifecycle is owned by :mod:`fleet_operations`, not by a ``with`` block, because
it outlives the request that paid for it.

This module is the seam between that lifecycle and a cloud API, and it is
deliberately three methods wide:

* ``create`` — make ONE instance for this operation, carrying the operation id
  into the instance's boot environment so the node can announce it back, and
  carrying the idempotency key to the provider so that a retried HTTP request
  cannot produce a second machine.
* ``find`` — "did this idempotency key produce an instance?" This is the method
  that makes ``uncertain`` resolvable. It MUST raise :class:`LookupUnavailable`
  when it cannot answer rather than returning None: "I could not check" and
  "nothing was created" are the two readings the whole ``uncertain`` state
  exists to keep apart, and a provider adapter that collapses them here has
  quietly re-introduced the double-create.
* ``terminate`` — idempotent delete.

:func:`run_provision` is the impure runner that binds a provider to the store.
Its one rule: **an unrecognised failure resolves to ``uncertain``, never to
``rejected``.** A create call that raised for a reason we did not model may well
have created something. Reading an unknown failure as "nothing happened" is how
a fleet pays for a machine it does not know it has.

Zero non-stdlib dependencies, like :mod:`provision` and for the same reason.
"""
from __future__ import annotations

import abc
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Mapping, Optional

from .fleet_operations import (
    ANNOUNCED, CREATED, CREATING, OperationStore, REJECTED, UNCERTAIN,
    Operation, structured_error,
)
from .provision import CapacityError, ProvisionError


class LookupUnavailable(ProvisionError):
    """``find`` could not reach the provider. NOT "nothing was created"."""


class RequestRejected(ProvisionError):
    """The provider refused the request and created nothing: a bad spec, an
    unknown image, a quota, a credential. Safe to treat as ``rejected`` because
    the provider said so — which is exactly what separates it from silence."""


class UncertainEffect(ProvisionError):
    """A create may or may not have happened — a timeout, a reset connection, a
    5xx after the request was accepted. Resolvable only by :meth:`WorkerProvider.find`."""


@dataclass(frozen=True)
class WorkerSpec:
    """What one reusable fleet worker should be.

    ``announce_env`` is the payload that makes correlation work: the runner adds
    ``LIVESTACK_OPERATION_ID`` to it, the boot script exports it, and
    ``livestack_node.serve`` announces it to the broker. Everything else here is
    ordinary cloud shape.
    """
    region: str
    instance_type: str
    name_prefix: str = "livestack-worker"
    image_family: str = "ubuntu_24_04_x64"
    image_id: Optional[str] = None
    system_disk_category: str = "cloud_essd"
    system_disk_gib: int = 80
    auto_release_hours: float = 4.0
    bootstrap: str = ""
    announce_env: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    # --- placement. Not optional in any modern account ---------------------
    #
    # `RunInstances` REQUIRES a security group, and a VPC instance requires a
    # vSwitch. Without them the call is refused, so a pool that omits them can
    # only ever produce a claimed operation that then fails — quota held, no
    # machine, and a `request_or_workload_fault` to read afterwards. They are
    # checked before the call instead; see `AliyunEcsWorkerProvider.validate_spec`.
    zone_id: Optional[str] = None
    vswitch_id: Optional[str] = None
    security_group_id: Optional[str] = None
    key_pair_name: Optional[str] = None
    # A worker with no public egress cannot reach a broker outside its VPC, so
    # it boots, never announces, and fails on its deadline WHILE BILLING. Zero
    # is a legitimate choice (mesh-joined or VPC-internal broker) and therefore
    # has to be stated rather than defaulted into.
    internet_charge_type: str = "PayByTraffic"
    internet_max_bandwidth_out_mbit: int = 0
    # --- price --------------------------------------------------------------
    #
    # Set from the pool's TIER by `fleet_pools.spec_for`. A pool declared SPOT
    # whose create omits `SpotStrategy` is billed at ON-DEMAND rates while the
    # planner scores it at the spot price it advertised — the scheduler then
    # prefers it *because* it is cheap, and only the invoice disagrees.
    spot_strategy: Optional[str] = None
    spot_price_limit: Optional[float] = None


class WorkerProvider(abc.ABC):
    """One cloud, three verbs. See the module docstring for why ``find`` is here
    and why it may not answer None on an error."""
    provider: str = "base"

    @abc.abstractmethod
    def create(self, *, operation_id: str, idempotency_key: str,
               spec: WorkerSpec) -> str:
        """Create ONE instance and return its provider id.

        Raise :class:`CapacityError` when the provider has no capacity (nothing
        created), :class:`RequestRejected` when it refused the request (nothing
        created), and :class:`UncertainEffect` when the outcome is unknown.
        """

    @abc.abstractmethod
    def find(self, idempotency_key: str) -> Optional[str]:
        """The instance id this key produced, or None if it provably produced
        none. Raise :class:`LookupUnavailable` if the provider cannot be asked."""

    @abc.abstractmethod
    def terminate(self, instance_id: str) -> None:
        """Idempotent delete."""

    def validate_spec(self, spec: WorkerSpec) -> List[str]:
        """Why this spec cannot produce an instance, or an empty list.

        Checked BEFORE the claim wherever possible, because the alternative is a
        claimed operation that holds its owner's quota and then fails on a
        parameter the broker could have seen was missing at startup.
        """
        return []


# --- the runner -------------------------------------------------------------
def run_provision(store: OperationStore, operation: Operation,
                  provider: WorkerProvider, spec: WorkerSpec, *,
                  now: Optional[float] = None,
                  log: Callable[[str], None] = lambda *_: None) -> Operation:
    """Call the provider for an operation the store has already moved to
    ``creating``, and record what came back.

    The order is the point and it is not negotiable: the store wrote
    ``creating`` — with the idempotency key — before this function ran. This
    function cannot be entered for an operation that is not already durable, and
    it never retries. A failure it cannot classify becomes ``uncertain``, whose
    only exit is :func:`reconcile`.
    """
    if operation.state != CREATING:
        raise ValueError(f"{operation.operation_id}: run_provision requires "
                         f"'creating', not {operation.state!r}")
    env = dict(spec.announce_env)
    # The correlation token. Written into the machine's boot environment so the
    # node repeats it back on announce; see `announce.node_operation_id`.
    env["LIVESTACK_OPERATION_ID"] = operation.operation_id
    spec = replace(spec, announce_env=env)
    try:
        instance_id = provider.create(operation_id=operation.operation_id,
                                      idempotency_key=operation.idempotency_key,
                                      spec=spec)
    except CapacityError as e:
        log(f"[operations] {operation.operation_id}: no capacity: {e}")
        return store.transition(
            operation.operation_id, REJECTED,
            reason=f"{provider.provider}: no capacity for this pool right now",
            error=structured_error("create", "capacity_shortage",
                                   "no_capacity", str(e)), now=now)
    except RequestRejected as e:
        log(f"[operations] {operation.operation_id}: request refused: {e}")
        return store.transition(
            operation.operation_id, REJECTED,
            reason=f"{provider.provider}: the request was refused; nothing was created",
            error=structured_error("create", "request_or_workload_fault",
                                   "refused", str(e)), now=now)
    except BaseException as e:  # noqa: BLE001 — see the module docstring
        # Everything else, UncertainEffect included, and deliberately including
        # failures nobody modelled. The expensive mistake is reading an unknown
        # failure as "nothing happened"; the cheap one is reconciling a create
        # that never occurred.
        log(f"[operations] {operation.operation_id}: create outcome UNKNOWN: {e}")
        return store.transition(
            operation.operation_id, UNCERTAIN,
            reason=(f"{provider.provider}: the create's effect is unknown; "
                    f"it will be reconciled, never retried"),
            error=structured_error("create", "uncertain_effect",
                                   type(e).__name__, str(e)), now=now)
    log(f"[operations] {operation.operation_id}: created {instance_id}")
    return store.transition(
        operation.operation_id, CREATED, provider_instance_id=instance_id,
        reason=f"{provider.provider} created {instance_id}", now=now)


def reconcile(store: OperationStore, operation_id: str,
              provider: WorkerProvider, *, now: Optional[float] = None) -> Operation:
    """Resolve an ``uncertain`` operation by asking the provider. Never creates."""
    return store.reconcile(operation_id, provider.find, now=now)


def recover_all(store: OperationStore, providers: Mapping[str, WorkerProvider], *,
                log: Callable[[str], None] = lambda *_: None) -> List[Operation]:
    """Startup: mark every mid-create operation ``uncertain``, then resolve the
    ones whose provider will answer. A provider that will not answer leaves its
    operation ``uncertain`` — which is correct, and which is why nothing here
    falls back to creating."""
    out: List[Operation] = []
    store.recover()
    for op in store.active():
        if op.state != UNCERTAIN:
            continue
        provider = providers.get(op.provider or "")
        if provider is None:
            log(f"[operations] {op.operation_id}: uncertain and no adapter for "
                f"provider {op.provider!r}; it stays uncertain and holds its quota")
            continue
        try:
            out.append(reconcile(store, op.operation_id, provider))
        except LookupUnavailable as e:
            log(f"[operations] {op.operation_id}: provider cannot be asked "
                f"({e}); it stays uncertain")
    return out


def announce_from_view(store: OperationStore, view: Mapping[str, object], *,
                       now: Optional[float] = None) -> List[Operation]:
    """Green every operation whose node is in the fleet view, carrying that
    operation's id, and ready.

    The correlation, done in one place. Note what is NOT here: no "a node
    appeared near the right time", no counting. A node that carries no
    operation id greens nothing, and a node carrying an id while not ready
    greens nothing either — being billed is not the same as being usable.
    """
    out: List[Operation] = []
    hosts = (view or {}).get("hosts") or {}
    for host in hosts.values():
        for node in (host or {}).get("nodes") or []:
            oid = node.get("operation_id")
            if not oid:
                continue
            op = store.announce(oid, ready=bool(node.get("ready")),
                                node=node.get("peer"), now=now)
            if op is not None and op.state == ANNOUNCED:
                out.append(op)
    return out


# --- Aliyun ECS -------------------------------------------------------------
#: The ECS RPC API version the request shapes here are written against — the
#: same one the TypeScript `aliyun` package uses, so the two cannot drift into
#: describing different APIs.
ECS_VERSION = "2014-05-26"
DEFAULT_ECS_ENDPOINT = "https://ecs.aliyuncs.com/"
#: Tag every instance so `find` has something to query by. `app` matches what
#: the TypeScript adapter already writes, so both see the same fleet.
TAG_APP = ("app", "livestack")


def percent_encode(value: str) -> str:
    """Aliyun's percent-encoding for the RPC signature: RFC 3986 unreserved set
    (``A-Za-z0-9-_.~``) and everything else escaped.

    Written out rather than assumed, because every way of getting it wrong
    produces the same single opaque symptom — ``SignatureDoesNotMatch`` — and no
    indication of which character did it. Note that this is NOT
    ``encodeURIComponent``: that leaves ``!'()`` literal, which the spec does not.
    """
    out = urllib.parse.quote(str(value), safe="~")
    return out.replace("+", "%20")


def string_to_sign(params: Mapping[str, str], method: str = "POST") -> str:
    """``METHOD&%2F&<canonical query>`` — the exact bytes that get signed.

    Exposed separately from :func:`sign_rpc` so a test can assert the STRING
    rather than only the digest. A digest assertion that fails tells you nothing
    about which of the three things went wrong; this one names it.
    """
    canonical = "&".join(
        f"{percent_encode(k)}={percent_encode(params[k])}" for k in sorted(params))
    return f"{method}&{percent_encode('/')}&{percent_encode(canonical)}"


def sign_rpc(params: Mapping[str, str], *, access_key_secret: str,
             method: str = "POST") -> Dict[str, str]:
    """The signed parameter set for one Aliyun RPC call: HMAC-SHA1 over
    :func:`string_to_sign` with the secret suffixed by ``&``, base64.

    The same scheme `signAliyunRpcRequest` in the `aliyun` TypeScript package
    implements. Pure — no network, no credential — so the encoding and the
    canonical ordering are testable on their own.
    """
    digest = hmac.new(f"{access_key_secret}&".encode(),
                      string_to_sign(params, method).encode(), hashlib.sha1).digest()
    return {**dict(params), "Signature": base64.b64encode(digest).decode()}


class AliyunEcsWorkerProvider(WorkerProvider):
    """Aliyun ECS on-demand / spot, as reusable fleet workers.

    Idempotency is enforced in TWO places on purpose. ``ClientToken`` makes the
    provider itself refuse to run a second instance for a repeated request, so a
    retried HTTP call at the network layer cannot bill twice; the
    ``livestack:operation`` tag makes :meth:`find` able to answer afterwards,
    which is what resolves an ``uncertain`` operation whose reply was lost
    before the token could help. The first prevents the duplicate; the second
    proves what happened.
    """
    provider = "aliyun"

    def __init__(self, *, access_key_id: Optional[str] = None,
                 access_key_secret: Optional[str] = None,
                 endpoint: Optional[str] = None,
                 region: Optional[str] = None,
                 timeout: float = 30.0,
                 transport: Optional[Callable[[str, bytes], str]] = None,
                 clock: Callable[[], float] = time.time,
                 nonce: Callable[[], str] = lambda: str(uuid.uuid4())):
        env = os.environ
        self._key = access_key_id or env.get("ALIBABA_CLOUD_ACCESS_KEY_ID") \
            or env.get("ALIYUN_ACCESS_KEY_ID")
        self._secret = access_key_secret or env.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET") \
            or env.get("ALIYUN_ACCESS_KEY_SECRET")
        self.endpoint = endpoint or env.get("ALIYUN_ECS_ENDPOINT") or DEFAULT_ECS_ENDPOINT
        self.region = region or env.get("ALIBABA_CLOUD_REGION_ID") \
            or env.get("ALIYUN_REGION_ID") or "cn-heyuan"
        self.timeout = timeout
        self._transport = transport or self._post
        self._clock = clock
        self._nonce = nonce

    # -- plumbing ------------------------------------------------------------
    def _credentials(self):
        if not self._key or not self._secret:
            raise RequestRejected(
                "set ALIBABA_CLOUD_ACCESS_KEY_ID / _SECRET (or the ALIYUN_ "
                "spellings); a missing credential is a refused request, not a "
                "capacity shortage")
        return self._key, self._secret

    def _post(self, endpoint: str, body: bytes) -> str:
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"content-type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read().decode()

    def call(self, action: str, params: Mapping[str, str]) -> dict:
        """One signed RPC call. Classifies its own failures, because the caller
        cannot: a 4xx from Aliyun means the request was refused and nothing was
        created; a timeout or a 5xx means we do not know."""
        key, secret = self._credentials()
        signed = sign_rpc({
            **dict(params), "Action": action, "Version": ECS_VERSION,
            "Format": "JSON", "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0", "SignatureNonce": self._nonce(),
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(self._clock())),
            "AccessKeyId": key,
        }, access_key_secret=secret)
        body = urllib.parse.urlencode(signed).encode()
        try:
            text = self._transport(self.endpoint, body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            code = _aliyun_code(detail)
            if code in _CAPACITY_CODES:
                raise CapacityError(f"ECS {action}: {code}: {detail}") from e
            if 400 <= e.code < 500:
                raise RequestRejected(f"ECS {action} {e.code}: {detail}") from e
            raise UncertainEffect(f"ECS {action} {e.code}: {detail}") from e
        except Exception as e:  # noqa: BLE001 — timeout, reset, DNS: unknown effect
            raise UncertainEffect(f"ECS {action}: {e}") from e
        return json.loads(text) if text.strip() else {}

    # -- the contract --------------------------------------------------------
    def validate_spec(self, spec: WorkerSpec) -> List[str]:
        """What ECS will refuse, said here instead of after a claim."""
        problems: List[str] = []
        if not spec.security_group_id:
            problems.append(
                "security_group_id is required: RunInstances is refused without one")
        if not spec.vswitch_id:
            problems.append(
                "vswitch_id is required: an instance with no vSwitch has no VPC to join")
        if not spec.instance_type:
            problems.append("instance_type is required")
        if not (spec.region or self.region):
            problems.append("region is required")
        return problems

    def create(self, *, operation_id: str, idempotency_key: str,
               spec: WorkerSpec) -> str:
        problems = self.validate_spec(spec)
        if problems:
            # Before any HTTP call, so nothing is billed and the operation is
            # classified as our fault rather than as a capacity shortage.
            raise RequestRejected(
                f"the pool's spec cannot produce an instance: {'; '.join(problems)}")
        params: Dict[str, str] = {
            "RegionId": spec.region or self.region,
            "InstanceType": spec.instance_type,
            "Amount": "1",
            "InstanceName": f"{spec.name_prefix}-{operation_id[-8:].lower()}",
            "HostName": spec.name_prefix,
            "SystemDisk.Category": spec.system_disk_category,
            "SystemDisk.Size": str(int(spec.system_disk_gib)),
            "AutoReleaseTime": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(self._clock() + spec.auto_release_hours * 3600)),
            # The provider's own idempotency guarantee. Without it a retry at
            # the transport layer is a second machine.
            "ClientToken": idempotency_key[:64],
            "UserData": base64.b64encode(
                bootstrap_script(spec).encode()).decode(),
        }
        if spec.image_id:
            params["ImageId"] = spec.image_id
        else:
            params["ImageFamily"] = spec.image_family
        # Placement. `validate_spec` has already refused a spec missing these.
        params["SecurityGroupId"] = spec.security_group_id or ""
        params["VSwitchId"] = spec.vswitch_id or ""
        if spec.zone_id:
            params["ZoneId"] = spec.zone_id
        if spec.key_pair_name:
            params["KeyPairName"] = spec.key_pair_name
        if spec.internet_max_bandwidth_out_mbit:
            params["InternetChargeType"] = spec.internet_charge_type
            params["InternetMaxBandwidthOut"] = str(
                int(spec.internet_max_bandwidth_out_mbit))
        # Price. Omitting this on a pool the planner scored as SPOT is the
        # expensive kind of silent failure: the bill disagrees with the plan.
        if spec.spot_strategy:
            params["SpotStrategy"] = spec.spot_strategy
            if spec.spot_price_limit is not None:
                params["SpotPriceLimit"] = str(float(spec.spot_price_limit))
        for i, (k, v) in enumerate(self._tags(operation_id, idempotency_key,
                                              spec).items(), start=1):
            params[f"Tag.{i}.Key"] = k
            params[f"Tag.{i}.Value"] = v
        result = self.call("RunInstances", params)
        ids = ((result.get("InstanceIdSets") or {}).get("InstanceIdSet") or [])
        if not ids:
            # A 200 with no instance is not a success and must not be read as
            # one; it is also not proof that nothing was created.
            raise UncertainEffect(
                f"ECS RunInstances returned no instance id: {json.dumps(result)[:300]}")
        return str(ids[0])

    def find(self, idempotency_key: str) -> Optional[str]:
        tags = {TAG_APP[0]: TAG_APP[1], "livestack:key": idempotency_key}
        params: Dict[str, str] = {"RegionId": self.region, "PageSize": "100"}
        for i, (k, v) in enumerate(tags.items(), start=1):
            params[f"Tag.{i}.Key"] = k
            params[f"Tag.{i}.Value"] = v
        try:
            result = self.call("DescribeInstances", params)
        except (UncertainEffect, RequestRejected, CapacityError) as e:
            # The one thing this method may never do is answer "nothing was
            # created" because it could not ask.
            raise LookupUnavailable(f"ECS DescribeInstances: {e}") from e
        rows = ((result.get("Instances") or {}).get("Instance") or [])
        return str(rows[0]["InstanceId"]) if rows else None

    def terminate(self, instance_id: str) -> None:
        try:
            self.call("DeleteInstances", {
                "RegionId": self.region, "InstanceIds.1": instance_id, "Force": "true"})
        except RequestRejected:
            # Already gone. Idempotent teardown is the contract, and a provider
            # that has forgotten the instance has satisfied it.
            return

    def _tags(self, operation_id: str, idempotency_key: str,
              spec: WorkerSpec) -> Dict[str, str]:
        return {TAG_APP[0]: TAG_APP[1],
                "livestack:operation": operation_id,
                "livestack:key": idempotency_key,
                "region": spec.region or self.region,
                "instanceType": spec.instance_type,
                **{str(k): str(v) for k, v in (spec.labels or {}).items()}}


#: Aliyun codes that mean "no stock", as opposed to "your request was wrong".
#: The distinction drives the whole escalation ladder: a capacity shortage is
#: retried elsewhere by the planner, a bad request is not.
_CAPACITY_CODES = frozenset({
    "OperationDenied.NoStock", "OperationDenied.ZoneNotAllowBuy",
    "ResourceNotAvailable", "InvalidInstanceType.ValueUnauthorized",
    "Resource.NotAvailable",
})


def _aliyun_code(detail: str) -> str:
    try:
        return str(json.loads(detail).get("Code") or "")
    except Exception:  # noqa: BLE001 — an unparseable body has no code
        return ""


def bootstrap_script(spec: WorkerSpec) -> str:
    """The instance's boot script: export the announce environment, then hand
    over to whatever the operator's image runs.

    Kept minimal and readable. The one line that MUST be here is the
    ``LIVESTACK_OPERATION_ID`` export — without it the node announces without
    its correlation token, the operation never greens, and the broker pays for a
    worker it cannot prove it asked for.
    """
    lines = ["#!/usr/bin/env bash", "set -euo pipefail",
             "mkdir -p /etc/livestack"]
    for k, v in sorted((spec.announce_env or {}).items()):
        lines.append(f"echo {_sh(f'{k}={v}')} >> /etc/livestack/worker.env")
        lines.append(f"export {k}={_sh(str(v))}")
    if spec.bootstrap:
        lines.append(spec.bootstrap)
    return "\n".join(lines) + "\n"


def _sh(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"
