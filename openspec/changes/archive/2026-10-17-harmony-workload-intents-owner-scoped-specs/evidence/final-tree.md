# Final-tree evidence

Tested source commit: `4f632f1b9cda5ea5ec76f014b4200ea7bca754fd`.
Git tree: `053ac986023628c4926b554aa3074a53a388c8a2`.
Base: `4a8860d4fde276ecb702f59882f269258ae0d34f` (`origin/main` on 2026-10-17).

## Focused integration

Command:

```sh
PYTHONPATH=node-py python3 -m unittest discover -s node-py/tests -p 'test_streams_workload_authority_integration.py' -v
```

Result: **3 tests passed in 2.783 seconds** on the tested source commit/tree above. The fixture uses temporary SQLite/CAS state, the production authority factory and default resolver, real WorkloadStore admission, and isolated framed ingress. It covers valid owner-scoped resolution and replay; malformed, missing, foreign-owner, wrong-media/size/digest, handler and input refusals with no job/attempt; unavailable backend attribution; and authorized/unauthorized cancellation outcomes.

## OpenSpec and diff validation

```sh
openspec validate harmony-workload-intents-owner-scoped-specs --strict
git diff --check
```

Both passed on the tested source commit/tree above. No service was started or reconfigured, and no production authority or worker was contacted.
