#!/usr/bin/env python3
"""Prove a provider credential works WITHOUT creating anything.

`DescribeInstances` is read-only and free, and it is the same call
`AliyunEcsWorkerProvider.find` makes — the one that has to work for an uncertain
create to be reconcilable. So this checks the exact path that matters, and the
worst it can do is list instances.

Run it BEFORE declaring a pool. A credential that cannot describe cannot
reconcile, and a fleet that can create but not reconcile is the one arrangement
this whole design exists to avoid.

    sudo -E env $(sudo cat /etc/livestack/fleet-provider.env | xargs) \
        python3 node-py/scripts/check_provider_credentials.py --region cn-heyuan

Prints no credential, ever. Exit 0 = the provider answered.
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--region", default=None, help="e.g. cn-heyuan")
    ap.add_argument("--provider", default="aliyun", choices=["aliyun"])
    args = ap.parse_args()

    from livestack_node.fleet_workers import (
        AliyunEcsWorkerProvider, LookupUnavailable, RequestRejected,
    )

    provider = AliyunEcsWorkerProvider(**({"region": args.region} if args.region else {}))
    # A key nothing was ever created with: the answer is "no instance", and
    # getting that answer is the whole proof. Never a create.
    probe = "livestack-credential-preflight-never-created"
    try:
        found = provider.find(probe)
    except LookupUnavailable as exc:
        # The one failure that matters. `find` refusing to answer is what makes
        # an uncertain create unresolvable, so it is reported as fatal here
        # rather than as a warning.
        print(f"REFUSED: the provider could not be asked — {_short(exc)}", file=sys.stderr)
        print("  A credential that cannot DescribeInstances cannot reconcile an\n"
              "  uncertain create. Do not declare a pool until this passes.",
              file=sys.stderr)
        return 1
    except RequestRejected as exc:
        print(f"REFUSED: {_short(exc)}", file=sys.stderr)
        return 1
    print(f"OK: {provider.provider} answered DescribeInstances in "
          f"{provider.region} (probe key found: {found!r})")
    print("Nothing was created. The credential can describe, so it can reconcile.")
    return 0


def _short(exc: BaseException) -> str:
    """The provider's message, bounded and never echoing a credential — the
    adapter does not put one in its errors, and this keeps it that way."""
    text = str(exc)
    return text[:300] + ("…" if len(text) > 300 else "")


if __name__ == "__main__":
    raise SystemExit(main())
