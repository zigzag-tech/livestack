# Upload-grant PUTs through the public edge relay

Off-mesh collaborators upload a one-use grant's bytes straight to the authority through the Osaka relay
(`harmony-edge.service`, port 8803 on the gateway VPS, `edge_forward.py`). The authority's
`public_base_url` is the origin only (`https://hs.zztech.io`; the schema refuses a path), so the gateway's
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
