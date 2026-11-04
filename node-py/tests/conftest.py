"""Keep the test session off real brokers.

`announce.broker_urls()` falls back to the local host broker (127.0.0.1:8799)
when LIVESTACK_BROKER_URL is unset, and importing the harmony-llm server starts
announcing. So running these tests on a production host registered the TEST
unit list with that host's real broker, under the production facade's address
(seen 2026-09-22 on xc-tower-ubuntu). Point the session at the discard port
unless the caller chose a broker on purpose; tests that exercise the default
(test_announce) set and restore the variable themselves.
"""
import os
import sys

os.environ.setdefault("LIVESTACK_BROKER_URL", "http://127.0.0.1:9")

# The same trap for the host-RAM ledger (host_ledger.py): it defaults to the
# user's real ledger, so the worker tests' attempts were written into the live
# one on zz-whitby (2026-10-18, worker "integration") where the host broker
# subtracts them. One throwaway ledger for the session unless a test chose one.
import tempfile as _tempfile
os.environ.setdefault("LIVESTACK_HOST_LEDGER_DIR",
                      _tempfile.mkdtemp(prefix="livestack-test-host-ledger-"))
# Same for measured load times: a fake engine's 2 s "load" in the real file
# would become the host's published `llm.load_s`.
os.environ.setdefault("HARMONY_UNIT_LOADS_FILE", os.path.join(
    _tempfile.mkdtemp(prefix="livestack-test-unit-loads-"), "unit-loads.jsonl"))

# And for hostd's ledger dir (decision ledgers and the exclusive-device lease
# store, device-exclusivity.sqlite3): seven test files build the hostd app
# without setting it, so on a production host they opened the LIVE lease store.
# With the boot-id store (96d0943e) that open releases every active lease as
# `previous_boot`, and a test broker's reconcile can drive the live rows too.
# Observed 2026-11-04 on xc-tower-ubuntu: an agent's `pytest tests` killed the
# history-scroll chain's FLUX leases at 10:27 and 10:31. Tests that need a
# specific dir still monkeypatch it.
os.environ.setdefault("LIVESTACK_LEDGER_DIR",
                      _tempfile.mkdtemp(prefix="livestack-test-ledger-"))

# The harmony-llm ENGINE package lives beside the server it drives
# (examples/harmony-llm/engines/). Engine-adapter tests import it directly, and
# the server itself bootstraps the same path when it is exec'd by file location.
_ENGINES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "harmony-llm")
if _ENGINES_ROOT not in sys.path:
    sys.path.insert(0, _ENGINES_ROOT)
