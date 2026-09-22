"""A fake reusable-worker provider that can misbehave the way real ones do.

The failures worth testing are not "the API returned an error" — they are the
ones where WE cannot tell what happened: a reply that never arrived after the
create was accepted, a process that died between writing `creating` and calling
out, a completion that lands after we gave up. Each of those has a switch here,
and `created` is the ledger the assertions read: **one billed create per logical
operation**, whatever was done to the caller in between.
"""
from typing import Dict, List, Optional

from livestack_node.fleet_workers import (
    LookupUnavailable, RequestRejected, UncertainEffect, WorkerProvider,
)
from livestack_node.provision import CapacityError


class FakeWorkerProvider(WorkerProvider):
    provider = "fake"

    def __init__(self, *, no_capacity=False, refuse=False, lose_reply=False,
                 lookup_down=False, late_completion=False):
        self.no_capacity = no_capacity        # provider has no stock (nothing created)
        self.refuse = refuse                  # provider refused the request (nothing created)
        self.lose_reply = lose_reply          # created, then the reply vanished
        self.lookup_down = lookup_down        # find() cannot reach the provider
        self.late_completion = late_completion  # the create lands only after the reply is lost
        #: instance_id -> idempotency key. THE assertion surface: len() is the bill.
        self.created: Dict[str, str] = {}
        self.terminated: List[str] = []
        self.create_calls: List[str] = []
        self._n = 0

    def create(self, *, operation_id, idempotency_key, spec):
        self.create_calls.append(idempotency_key)
        if self.no_capacity:
            raise CapacityError("fake: no stock in this pool")
        if self.refuse:
            raise RequestRejected("fake: bad image id")
        # The provider's own idempotency: a repeated key never bills twice, which
        # is what makes "one billed create" an invariant of the pair rather than
        # a property of the caller being careful.
        for iid, key in self.created.items():
            if key == idempotency_key:
                return iid
        self._n += 1
        iid = f"fake-i-{self._n}"
        if self.lose_reply:
            if not self.late_completion:
                self.created[iid] = idempotency_key
            else:
                # The machine appears only after we have given up waiting — the
                # case that makes retrying an unknown create so expensive.
                self._pending = (iid, idempotency_key)
            raise UncertainEffect("fake: the connection reset after the create")
        self.created[iid] = idempotency_key
        return iid

    def settle(self):
        """The late create finally lands. Called by a test between the lost reply
        and the reconcile."""
        pending = getattr(self, "_pending", None)
        if pending:
            self.created[pending[0]] = pending[1]
            self._pending = None

    def find(self, idempotency_key):
        if self.lookup_down:
            raise LookupUnavailable("fake: the provider API is unreachable")
        for iid, key in self.created.items():
            if key == idempotency_key:
                return iid
        return None

    def terminate(self, instance_id):
        self.terminated.append(instance_id)
        self.created.pop(instance_id, None)
