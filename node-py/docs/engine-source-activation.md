# Activate a committed Strata source

`tools/activate-engine-source.py` is the operator command for updating a
Strata unit's source pin and runtime selection together. It does not load a
model, choose a different unit or change the unit's placement policy.

Prepare a dedicated clean Git checkout at the full reviewed revision and a
verified artifact directory. Its `receipt.json` must name that source revision
in `source` and contain `files.strata` and `files.libstrata_kq.so`, each with
`bytes` and `sha256`. Keep the existing model files and interpreter available.
The interpreter argument is the virtualenv's `bin/python`, not its resolved
system-Python target. The source checkout must be readable by the service user.

Capture the current declaration file's SHA-256 and the existing unit source
revision. Start with a read-only plan:

```sh
python tools/activate-engine-source.py \
  --units-file /etc/harmony/llm-units.json --unit flash_next \
  --expected-source-rev '<current declared revision>' \
  --expected-units-sha256 '<current file SHA-256>' \
  --source-root /path/to/clean/reviewed/Strata \
  --source-rev '<full reviewed Git SHA>' \
  --artifacts /path/to/verified/native/receipt.json \
  --python /path/to/existing/venv/bin/python
```

To apply that plan, add:

```sh
  --apply --operation-dir /private/activation-receipts/new-operation \
  --broker-url http://127.0.0.1:8799 \
  --token-file /private/existing-broker-credential \
  --device-id '<the facade device ID>' --owner '<authorized operator owner>' \
  --service harmony-llm.service --wait-seconds 120
```

The operator needs write access to the declaration, candidate checkout and
receipt directory, and permission to restart the explicitly selected service.
The receipt parent must already exist as a dedicated private directory. Never
put a credential value in arguments, shell variables or output.

Apply acquires the host broker's exclusive-device lease. Busy speech, training
or other reservations wait or refuse through the broker; the command never
cancels them. Once admitted it revalidates the entire plan, prepares the
candidate's interpreter link, real `STRATA_VERSION` and private run config,
then atomically changes only the selected unit's source fields. Other units,
model identity, placement requirements and residency policy remain intact.
The service restarts under the device lease. Normal requests cause subsequent
model residency; this command never invokes a warm or engine-load endpoint.

Failure before selection leaves the live declaration unchanged. A failed
restart restores the exact previous declaration only if the candidate still
owns the declaration digest. A concurrent deployment prevents rollback from
overwriting its changes. A failed previous-service restart or unconfirmed
lease release remains a failure in the journal. There is no automatic retry.

Each operation exclusive-creates one directory containing `journal.json`
(at most 64 KiB) and `previous-units.json` (at most 4 MiB), mode 0600. Its
dedicated parent accepts at most eight operations and then refuses: retain or
export the evidence and deliberately remove obsolete operations before reuse.
The per-declaration lock and atomic temporary file have fixed names. A stale
temporary file refuses replacement; investigate its operation before cleanup.
Input declarations are bounded to 64 units, artifacts to 2 GiB each, credential
reads to 2 KiB, broker replies to 64 KiB, admission to 1–480 seconds, heartbeat
and release requests to ten seconds, and each restart to 60 seconds.

A `completed` receipt proves selection and a successful service restart under
a lease. It does not prove model identity readback, healthy inference, native
GPU cessation, model quality, an application E2E verdict or a production release.
Run those controls separately and retain their actual receipts.
