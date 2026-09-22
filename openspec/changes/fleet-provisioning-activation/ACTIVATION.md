# Activating provisioning on the fleet broker

Everything below is prepared. The only missing input is a provider credential,
and it is deliberately the last thing added: a credential plus a pool
declaration together are what turn a planning process into a spending one.

Host: `xc-tower-ubuntu` (`100.64.0.18`), service `livestack-fleetd.service`,
port 8801. Current state: release `fleet-provisioning-6de47145` is live,
`pools: []`, so the broker **cannot provision**.

## 0. What credential, and what it should be allowed to do

The fleet broker needs exactly three ECS actions in one region:

| Action | Used by |
|---|---|
| `RunInstances` | `AliyunEcsWorkerProvider.create` |
| `DescribeInstances` | `.find` — the reconciliation that makes "one billed create" true |
| `DeleteInstances` | `.terminate` — drain |

**Do not reuse the render pipeline's key.** `~/.aliyun/unchain-render.env` (the
convention named in `unchain/unchain/docs/mjyt-pipeline-and-render-runbook.md`
and three sibling runbooks) carries OSS access as well, and it was minted for a
different blast radius. Mint a RAM user scoped to those three ECS actions in the
one region the pool names, and give it a spend ceiling at the account level. If
this key leaks, the damage should be "somebody rented machines in cn-heyuan",
not "somebody read the content store".

The adapter reads `ALIBABA_CLOUD_ACCESS_KEY_ID` / `ALIBABA_CLOUD_ACCESS_KEY_SECRET`,
falling back to the `ALIYUN_ACCESS_KEY_ID` / `_SECRET` spellings.

## 1. Install the credential (never as `Environment=`)

`Environment=` lines are visible in `systemctl show` and in the journal. Use an
`EnvironmentFile` that root owns and only root can read:

```bash
sudo install -m 0600 -o root -g root /dev/null /etc/livestack/fleet-provider.env
sudo tee /etc/livestack/fleet-provider.env >/dev/null <<'ENV'
ALIBABA_CLOUD_ACCESS_KEY_ID=…
ALIBABA_CLOUD_ACCESS_KEY_SECRET=…
ENV
```

The drop-in that reads it is already written and inert until the file exists:
`/etc/systemd/system/livestack-fleetd.service.d/80-provider-credentials.conf`.

**`sudo -e` needs a real TTY.** Run it from an ordinary shell on the host, not
through Claude Code's `!` prefix — `!` is non-interactive and the editor exits
with *Standard input is not a terminal*, leaving a 0-byte file. Either edit it in
a real terminal, or write it host-to-host without displaying it:

```bash
ssh <holding-host> 'sudo grep -h "^ALIBABA_CLOUD_ACCESS_KEY" ~/.aliyun/unchain-render.env'   | sudo tee /etc/livestack/fleet-provider.env >/dev/null
```

The file is read at process start, so `sudo systemctl restart livestack-fleetd`
afterwards or the running process still has nothing.

## 1b. Prove the credential works before declaring a pool

```bash
sudo systemctl restart livestack-fleetd
python3 node-py/scripts/check_provider_credentials.py --region cn-heyuan
```

It calls `DescribeInstances` — read-only, free, and the exact call
`AliyunEcsWorkerProvider.find` makes. That call is what resolves an uncertain
create, so a credential that can create but not describe produces precisely the
state this design exists to avoid: a machine that is billing and cannot be
reconciled. **Do not declare a pool until this passes.**

## 2. Declare ONE pool, small

This is the line that grants spending authority. Start with a ceiling you would
not mind paying in full by accident.

```bash
sudo tee /etc/systemd/system/livestack-fleetd.service.d/90-pools.conf >/dev/null <<'CONF'
[Service]
Environment='LIVESTACK_FLEET_POOLS=[{"id":"heyuan-spot","provider":"aliyun","tier":"SPOT","region":"cn-heyuan","instance_type":"ecs.g8i.2xlarge","cost_per_hour":<¥/h>,"max_instances":1,"kinds":["asr"]}]'
Environment='LIVESTACK_FLEET_WORKER_ENV={"LIVESTACK_BROKER_URL":"http://100.64.0.18:8801"}'
CONF
sudo systemctl daemon-reload && sudo systemctl restart livestack-fleetd
```

`LIVESTACK_FLEET_WORKER_ENV` must be an address a machine **in that region** can
reach. A mesh IP works only if the instance joins the mesh; otherwise use the
public endpoint. Get this wrong and the instance boots, never announces, and the
operation fails on its deadline with the instance still billing — which the
broker will tell you, but only after it has paid for it.

## 3. Verify, in this order

```bash
# the pool is in force, and an adapter exists for its provider
curl -s :8801/fleet | python3 -c 'import json,sys; print(json.load(sys.stdin)["pools"])'
# -> [{"id":"heyuan-spot",…,"adapter":true}]

journalctl -u livestack-fleetd --since -1min | grep '\[fleet\]'
# -> "1 elastic pool(s): heyuan-spot SPOT aliyun/cn-heyuan … ¥…/h x1"
```

Then drive one operation end to end (task 1.2), and confirm the joins:

```bash
curl -s :8801/fleet/operations
curl -s ':8801/fleet/ledger?limit=50' | python3 -m json.tool | grep -c '"decision": "operation"'
```

## 4. Rollback, at every step

- Pools: `sudo rm /etc/systemd/system/livestack-fleetd.service.d/90-pools.conf`
  → the broker plans and admits exactly as before and cannot provision.
- Credential: `sudo rm /etc/livestack/fleet-provider.env` → every create is
  refused as `request_or_workload_fault`, which is the correct classification —
  a missing credential is a refused request, not a capacity shortage.
- Whole release: `sudo rm /etc/systemd/system/livestack-fleetd.service.d/70-fleet-provisioning.conf`
  → back to `asr-preferences-2a3aa701`.

Each is one file and a restart.

## 5. What is still NOT granted by any of this

The incident classifier stays in `shadow`. It records a selection and acts on
nothing, and promotion to `serve` needs a qualification receipt that does not
exist — see `fleetd/receipts/README.md`.
