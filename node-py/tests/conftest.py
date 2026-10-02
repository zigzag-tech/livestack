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

# The harmony-llm ENGINE package lives beside the server it drives
# (examples/harmony-llm/engines/). Engine-adapter tests import it directly, and
# the server itself bootstraps the same path when it is exec'd by file location.
_ENGINES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "harmony-llm")
if _ENGINES_ROOT not in sys.path:
    sys.path.insert(0, _ENGINES_ROOT)
