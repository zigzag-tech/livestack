# Closing `/v1/classifier`

`POST /v1/classifier` on `xc-tower-ubuntu:8188` spends the resident 27B and had
no credential check. Measured 2026-09-22: **1,697 calls in 24 h from one
off-fleet host at a public address** (`public-la` = `89.208.242.179` =
`benchday.zztech.io`, the benchday hub classifying pane attention), bursting to
71 requests per second, **356 consecutive samples all `credential=none
presented`**.

The code is deployed and currently in the OFF position: no principal source is
configured on `harmony-llm-gpu0`, so behaviour is unchanged.

## Why a FIXED principal, not the existing `hub` one

`hub` is a DELEGATING principal (`delegate_prefix: "acct_"`). A delegating
principal has no identity of its own, so `fleet_auth.resolve_owner` refuses it
with **400** when the caller names no owner:

> `'hub' is a delegating principal and must name an `owner`; it has no identity
> of its own to charge`

And the classifier path names none — `pane-status-tick.ts:277` calls
`classifyPaneStatus(state)` with no `accountId`, so `identityHeaders` emits no
`x-harmony-owner`. Reusing the `hub` token would have 400'd every call.

So a fixed principal was minted: **`benchday-hub-classifier`**, owner
`benchday-hub`. Verified before anything depends on it — no owner header resolves
to `owner='benchday-hub'`; no credential and a wrong token both give 401.

It is also narrower than reusing `hub`, which can act for *any* `acct_*`.

## The order, and why it is not optional

The hub sends nothing today. Enforcing first would 401 every call and silently
degrade every `needs_you` flag in the fleet.

### Step 1 — the hub — **DONE 2026-09-22 08:41 UTC**

Installed on `public-la` (`root@100.64.0.19`; the `ubuntu` user is refused, which
is what made me wrongly report no access). Backup
`/etc/benchday/hub.env.bak-classifier-auth-20260922T084131Z`, one
`BENCHDAY_FLEET_TOKEN` line, hub restarted, active, zero errors. The transfer
copy of the token has been shredded; the principal table remains at
`/etc/harmony/classifier-principals.json` (`0600 root:root`).

For reference, the commands used:

```bash
sudo cp /etc/benchday/hub.env /etc/benchday/hub.env.bak-classifier-auth-$(date -u +%Y%m%dT%H%M%SZ)
printf 'BENCHDAY_FLEET_TOKEN=%s\n' '<token>' | sudo tee -a /etc/benchday/hub.env >/dev/null
sudo systemctl restart benchday-hub
```

`<token>` is in `/etc/harmony/classifier-token.txt` on `xc-tower-ubuntu`
(`0600 root:root`) — `sudo cat` it once, paste, then delete the file.

**`BENCHDAY_FLEET_TOKEN` is read by three subsystems**, so setting it is not
classifier-only: `decisions/jev-config.ts` (this), `fleet_roster_reader.ts` (polls
`:8799/fleet`, which reports `auth.required: false`, so a token is ignored there)
and `speech_http.ts`. The first two are safe; the speech path should be checked
if it authenticates anywhere.

### Verify step 1 before step 2

On `xc-tower-ubuntu`, the line that made this knowable:

```bash
journalctl -u harmony-llm-gpu0 -f | grep '\[classifier\]'
# was:  auth=off credential=none presented
# want: auth=off credential=unknown(<fingerprint>)
```

`unknown(...)` is the CORRECT intermediate state: the hub is presenting a
credential and the server has no table yet to resolve it. That is the proof step
2 is safe.

### Step 2 — the server (mechanical once step 1 is verified)

**The target changed.** `/v1/classifier` on `:8188` is served by
**`harmony-llm.service`**, not `harmony-llm-gpu0` — and `harmony-llm-gpu0` no
longer exists: another session removed its unit at 04:38:04
(`sudo rm -f /etc/systemd/system/harmony-llm-gpu0.service`), retiring the second
LLM slot on card 0. Its orphaned drop-in has been cleaned up.

Note also that the running `harmony-llm` process started at 04:05, BEFORE the
auth commit, so it does not yet carry the check; the restart below is what
picks it up.

```bash
sudo install -d /etc/systemd/system/harmony-llm.service.d
sudo tee /etc/systemd/system/harmony-llm.service.d/60-classifier-auth.conf >/dev/null <<'CONF'
[Service]
Environment=LIVESTACK_FLEET_TOKENS_FILE=/etc/harmony/classifier-principals.json
CONF
sudo systemctl daemon-reload && sudo systemctl restart harmony-llm
# want: auth=REQUIRED credential=benchday-hub-classifier
```

**BLOCKED until the hub calls again.** gpu0 served `:8190`, and that was the
endpoint the hub had resolved; it now reads `state=mia` and the hub logs
`ranking disabled: no typed-decision endpoint configured`. Zero classifier calls
have arrived since 04:38. Enforcing against a caller that cannot be observed is
the same blind flip this ordering exists to prevent, so step 2 waits until the
hub re-resolves onto `:8188` and the log shows `credential=unknown(...)`.

The path itself is healthy: a POST from the hub to `:8188/v1/classifier`
returned **200 in 0.58 s**.

## Rollback

Delete `60-classifier-auth.conf` and restart — back to `auth=off`, one file.
Step 1 is independently harmless: a token sent to a server that ignores it.
