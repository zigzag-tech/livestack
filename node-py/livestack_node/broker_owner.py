"""Optional broker admission accounting; not proof of predecessor quiescence.

Installed owner bootstrap supplies the registered authorizer. The gate never
turns a timed-out remote dispatch or an in-memory placement TTL into settlement.
Remote uncertainty intentionally requires a separate owner reconciliation path.
"""
from concurrent.futures import Executor
from functools import wraps
from dataclasses import asdict, is_dataclass
from hashlib import sha256
import json

from .provider_fence import ProviderFence, FenceRefused


class BrokerOwnerGate:
    def __init__(self, broker, authorize, *, journal=None):
        self.broker = broker
        self.fence = ProviderFence(authorize)
        self.journal = journal
        if journal is not None:
            for _row in journal.unsettled():
                key = self.fence.admit()
                self.fence.settle(key, False)

    def _admit(self, kind, peer_id, args, kwargs):
        with self.fence._lock:
            if self.fence._uncertain:
                raise FenceRefused('broker_owner_unsettled_reconciliation_required')
            key = self.fence.admit()
        identity = None
        try:
            if self.journal is not None:
                def encode(value):
                    if is_dataclass(value):
                        return asdict(value)
                    raise TypeError('broker_owner_closed_serializable_inputs_required')
                payload = json.dumps({'args': args, 'kwargs': kwargs}, sort_keys=True,
                    separators=(',', ':'), allow_nan=False, default=encode).encode()
                identity = {'kind': kind, 'peerId': peer_id, 'requestSha256': sha256(payload).hexdigest()}
                self.journal.intent(key, identity)
        except BaseException:
            self.fence.settle(key)
            raise
        return key, identity

    def _settle(self, key, identity, settled, *, not_dispatched=False):
        if self.journal is not None and settled:
            try:
                if not_dispatched:
                    self.journal.known_not_dispatched(key, identity)
                else:
                    self.journal.settle_local(key, identity)
            except BaseException:
                self.fence.settle(key, False)
                raise
        self.fence.settle(key, settled)

    def run(self, fn, *args, **kwargs):
        key, identity = self._admit('local-work', fn.__name__, args, kwargs)
        completed = False
        try:
            result = fn(*args, **kwargs)
            completed = True
            return result
        finally:
            # A local handler returning is not a remote model-load barrier.
            # Keep uncertainty sticky even if snapshot later expires a load.
            self._settle(key, identity, completed and not self.broker._in_flight)

    def executor(self, executor):
        return BrokerOwnedExecutor(self, executor)

    def install_dispatch(self):
        if getattr(self.broker, '_owner_gate', None) is not None:
            raise FenceRefused('broker_owner_gate_already_installed')
        self.broker._owner_gate = self
        peers = tuple(self.broker.peers)
        # Includes the proactive daemon's planning and allocator writes, not
        # just HTTP request lifetimes. Install before starting the daemon.
        for name in ('plan_and_apply', 'sweep_leaks'):
            original = getattr(self.broker, name)

            @wraps(original)
            def owned(*args, _original=original, **kwargs):
                if len(self.broker.peers) != len(peers) or any(
                    actual is not expected for actual, expected in zip(self.broker.peers, peers)):
                    raise FenceRefused('broker_owner_peer_inventory_changed')
                return self.run(_original, *args, **kwargs)

            setattr(self.broker, name, owned)

        for peer in peers:
            for name in ('warm', 'evict', 'reclaim'):
                original = getattr(peer, name, None)
                if original is None:
                    continue

                @wraps(original)
                def remote(*args, _original=original, _kind=name, _peer_id=getattr(peer, 'base', None), **kwargs):
                    key, identity = self._admit(_kind, _peer_id, args, kwargs)
                    settled = False
                    try:
                        return _original(*args, **kwargs)
                    except BaseException as error:
                        # Existing transport's explicit before-dispatch marker
                        # is absence evidence. An HTTP reply alone is NOT a
                        # physical accelerator barrier; even success stays held.
                        settled = getattr(error, 'dispatched', None) is False
                        raise
                    finally:
                        self._settle(key, identity, settled, not_dispatched=settled)

                setattr(peer, name, remote)


class BrokerOwnedExecutor(Executor):
    """Accepted underlying futures retain ownership after caller cancellation."""
    def __init__(self, gate, executor):
        self.gate, self.executor = gate, executor

    def submit(self, fn, /, *args, **kwargs):
        key, identity = self.gate._admit('local-work', fn.__name__, args, kwargs)
        try:
            future = self.executor.submit(fn, *args, **kwargs)
        except BaseException:
            self.gate._settle(key, identity, True, not_dispatched=True)
            raise

        def finished(done):
            settled = done.cancelled() or (
                done.exception() is None and not self.gate.broker._in_flight)
            self.gate._settle(key, identity, settled, not_dispatched=done.cancelled())

        future.add_done_callback(finished)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        return self.executor.shutdown(wait=wait, cancel_futures=cancel_futures)
