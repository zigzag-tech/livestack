# Final-tree evidence

Base source: `2ddaba7aad228a4ca617e296328a8716e5d2a063` (`origin/main` on 2026-10-17).

## Focused integration

Command:

```sh
PYTHONPATH=node-py python3 -m unittest discover -s node-py/tests -p 'test_streams_workload_authority_integration.py' -v
```

Result: **3 tests passed**. The fixture uses temporary SQLite/CAS state, the production authority factory and default resolver, real WorkloadStore admission, and isolated framed ingress. It covers valid owner-scoped resolution and replay; malformed, missing, foreign-owner, wrong-media/size/digest, handler and input refusals with no job/attempt; unavailable backend attribution; and authorized/unauthorized cancellation outcomes.

## OpenSpec and diff validation

```sh
openspec validate harmony-workload-intents-owner-scoped-specs --strict
git diff --check
```

Both passed. No service was started or reconfigured, and no production authority or worker was contacted.
