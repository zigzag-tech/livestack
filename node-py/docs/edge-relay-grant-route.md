# Upload-grant PUTs through the public edge relay

Off-mesh collaborators upload a one-use grant's bytes straight to the authority through the Osaka relay
(`harmony-edge.service`, port 8803 on the gateway VPS, `edge_forward.py`). The authority's
The principal that mints public grants carries `upload_base_url` (an origin, e.g. `https://hs.zztech.io`; a path is refused), so the gateway's
Caddy sends exactly one URL shape to the relay:

    @grantput path_regexp ^/v1/workloads/upload-grants/[0-9a-f]{32}/objects/[0-9a-f]{64}$
    handle @grantput { reverse_proxy 127.0.0.1:8803 { flush_interval -1 } }

placed before `handle_path /harmony-relay/*` in the `hs.zztech.io` block of `/etc/caddy/Caddyfile`.
Everything else on that host is unchanged (Headscale catch-all). The relay accepts only PUT on that
shape (other methods 405), refuses cheaply before reading a body, rate-limits per IP/globally, and lets the
authority refuse a wrong capability at header time (docs: edge_forward.py module docstring, tests in
tests/test_workload_edge_forward.py).

Reproduce/rollback (2026-10-06): backup `/etc/caddy/Caddyfile.bak-pre-grants-route`; revert = copy it back,
`caddy validate`, `systemctl reload caddy`. Relay revert: `/usr/local/sbin/revert-harmony-edge-relay`.
Budget alerts (50/80/100% of `budget_bytes`) are `ERROR` log lines `edge relay budget ALERT`.

## Worker hosts need a compilation-policy enrollment

A worker host is admitted only if `/etc/livestack/compilation-policy.json` (the authority's operator host
policy) names it. A new host with no compilation classes still needs an entry in BOTH `hosts` (empty class list)
and `enrollments` (its worker `host` string maps to the physical host), or the authority refuses every
worker call with `compilation_policy_unknown_enrollment` (the worker then loops "waiting").
2026-10-06: added `histo-one` (hosts: `[]`, enrollments: `histo-one -> histo-one`) for the askafox deploy worker.
Backup: `/etc/livestack/compilation-policy.json.bak-pre-histo-one-20261006`. Revert: copy it back (the authority re-reads
the file; no restart).

## Never set the global `public_base_url` when several principals mint grants

2026-10-06 incident: a server-wide `public_base_url` rewrote the upload URL of EVERY grant-minting principal. The benchday
source publisher validates the URL against the authority's own address and refused every grant for ~30 minutes. The authority
now refuses to start with a global `public_base_url` and more than one `upload_grants` principal
(`check_grant_origins`); put `upload_base_url` on the one principal that needs a public address instead.
