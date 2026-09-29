# Worker host pressure (workers inside a VM)

A Harmony worker reports `available.memory_bytes` from `/proc/meminfo`. Inside a
VM that is the **guest's** view: on 2026-09-28 a Lima guest reported ~15 GB
available while a leaking emulator held 32 GB of its 36 GB macOS host. The host
swapped, the guest missed lease renewals, and every attempt ended
`worker declared an infrastructure outcome (authority gave no reason)`, with only
`URLError`/`TimeoutError` in `worker.log`.

Config (opt-in; absent means unchanged behaviour):

```json
"host_pressure": {"path": "/Users/me/.local/state/host-pressure.json", "max_age_seconds": 120}
```

The file is `{"ts": <epoch seconds>, "available_memory_bytes": <int>}`, written by
the host. The worker reports `min(guest figure, file figure)`. A missing, stale
or malformed file reports **0** (never "no limit") and logs once per state change
(`host pressure reading stale|malformed|unreadable|ok`).

macOS publisher: `scripts/publish-macos-host-pressure.sh --install` (LaunchAgent,
every 15 s). It publishes reclaimable pages, or 0 while swapins+swapouts exceed
`SWAP_PAGES_PER_SEC` (500). Swap *used* is not the signal — macOS never shrinks it.
A Lima guest sees the host home read-only at `/Users/<user>`, so no extra mount.
