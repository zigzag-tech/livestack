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

os.environ.setdefault("LIVESTACK_BROKER_URL", "http://127.0.0.1:9")
