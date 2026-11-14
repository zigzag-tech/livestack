# Rank-only runtime startup

Benchday's actual catalog declares `harmony.fleet/1` daemon-owned and its ingress
refuses host-service registration for that contract. The earlier combined
`fleet_reader` attachment also selected this registration before ranking, so
its simulated-peer startup pass did not prove real ingress compatibility.

An independent optional `fleet_rank_reader` attachment now supplies ranking
input without selecting fleet publication. It uses the unchanged real runtime
start/stop, reconnecting SDK and periodic ranking task. Legacy combined reader
attachments retain the existing fallback. No daemon admission rule, catalog,
ranking vocabulary or publication payload was weakened or changed.

Worker: node-f, private snapshot
`/home/ubuntu/agent-builds/livestack-rank-startup-20261114/node-py`.
Command: `python3 -B -m unittest discover -s tests -p 'test_*stream*.py' -v`.
Result: **65 passed, 0 failed**, 21.869 seconds. Log SHA-256:
`bf02b42d2395fb08b76da2240b5ecd6fa5c4a5e8f7dc79e924e46de3de40b19c`.
Strict change validation and whitespace checks pass. The new startup control
uses the real SDK over Unix with a simulated peer; it proves contract selection
and actual task publication, not real daemon authority or Hub freshness.

Repin the Benchday fixture from the committed source, use the rank-only
attachment, and execute its real ingress/Broker composition regression before
claiming that integration passes. Production activation, source landing and
archive remain open.
