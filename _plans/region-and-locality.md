# Region and locality — where work is allowed to run

Status: **landed**. Nodes declare their region; the fleet view and the ranking
carry it; a caller states a policy and the broker applies it. 30 tests
(`node-py/tests/test_region_policy.py`, `test_preload.py`), plus the whole
node-py suite, green on xc-tower-ubuntu.

## The problem this solves

The fleet already ranked by **measured distance**, and from xc-tower-ubuntu the
numbers are stark:

| node | probe |
|---|---|
| xc-tower-ubuntu (same host) | 2.7 ms |
| xc-mac-studio | 6.6-19 ms |
| zz-tower0 | 546 ms |

Distance almost sorts the continent out by itself. "Almost" is the problem:
distance answers *how far from here*, never *where is it*, and the two come
apart the moment the asker moves — zz-tower0 is 2 ms away from a caller in
Nanjing. A production rule like "nothing a person watches is generated on the
dev box in Nanjing" is a statement about **where a machine is**, which no
measurement can make.

## The three parts, and who owns each

**The node declares where it is.** `LIVESTACK_NODE_REGION` (free-form; the
fleet's vocabulary today is `na` and `cn`). It travels two ways, and both are
needed:

- in the **announce** (`announce.register_once`), which reaches the brokers
  the node was configured with — in practice its own host's;
- in **`/livestack/capability`**, which is how a fleet broker on *another*
  host learns a node at all. Remote nodes are seeded and probed, never
  announced, so with the announce alone every remote node reported
  `region=None` while the broker's own host reported `na`.

Unset is `None` and a region filter must read that as *excluded*: a node that
has not said where it is has not said it is here.

**The broker reports it and ranks without it.** `fleet_view` carries the
region; `fleet_rank` puts it on every target row and orders on distance band,
then load, then id. A test asserts the invariant directly — the same fleet with
every region stripped ranks identically. A broker that ordered on region would
be deciding policy it has no account for.

**The caller states the policy.** Two ways, same rule:

```python
from livestack_node.client import choose
choose("polytts", allow_regions={"na"})     # raises NoEligibleTarget otherwise
```

```
GET /fleet/rank?kind=polytts&regions=na
```

`regions=` is applied by the broker *on the caller's behalf*: the answer is a
function of the request and changes when the request does, which is not the
same as the broker holding the policy. It exists so there is one implementation
of the filter instead of one per language — attune is TypeScript, and without
it that consumer would carry its own list of which hosts are North American.

`region=` (singular) is unchanged: the asker's own region, recorded on the
decision and never applied.

## Locality is a second constraint, and also the caller's

Some work cannot be placed at all. polyasr's `/v1/align/manifest` names blobs
by hash and opens them from the **caller's** `data/blob-cache`, so an ASR on
another machine is handed paths that do not exist there — measured, as
"audio blob sha256 17ea… was not present at data/blob-cache/sha256/17/", when
an authoring run on xc-mac-studio was given the ASR on xc-tower-ubuntu.

Only the caller knows its work reads its own disk, so the caller says so.
attune's client does it by filtering the ranked targets to one host
(`resolve('polyasr', { sameHost })`); `ComputeRequirement.locality` is the
typed form of the same idea for LLM placement.

A node's fleet id is **not** its hostname — it announces the id its service was
configured with. xc-mac-studio is `xc-mac-studio` on the fleet and
`Xiyangs-Mac-Studio.local` to `hostname()`, and the first pass that assumed
otherwise refused every ASR on the machine it was running on.

## Warm after the bind

Related, and found while bringing this up: a node that loads its model inside
its web framework's startup hook never binds, so it cannot be snapshotted, so
the broker never learns its kinds — and `admit` for an unknown kind is a
refusal, by design. The warm then waits for a grant that cannot come.
`attach(preload=...)` runs the warm on its own thread once the facade answers.
While it warms the node is `ready: false`, which the ranker already filters on.

## Operating it

Each node's unit or plist sets the region:

```ini
# /etc/systemd/system/<unit>.service.d/60-region.conf
[Service]
Environment=LIVESTACK_NODE_REGION=na
```

```bash
# macOS
/usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:LIVESTACK_NODE_REGION string na" \
  ~/Library/LaunchAgents/<label>.plist
```

Check it: `curl -s http://100.64.0.18:8801/fleet | jq '.hosts[].nodes[] | {peer,kinds,region,probe_ms}'`.
Every node should name a region; a `None` is a node nobody told.
