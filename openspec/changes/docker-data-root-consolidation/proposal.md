## Why

Rootless per-slot Docker data-roots (`docker_cache`) hold the same image layers once per worker and per
principal. Measured read-only on 2026-11-06 (layerdb diff-ids, `~/.cache/livestack-docker-cache`):

| host | data-roots | on disk | image layers (layerdb) | of which redundant | redundant across workers |
|---|---|---|---|---|---|
| zz-joe (e2e-1..6, 3 principals each) | 12 populated namespaces | 349 GB | 149.5 GB | 35.6 GB (24%) | 21.2 GB |
| zz-whitby (e2e-1, e2e-2) | 2 | 68 GB | 32.1 GB | 2.5 GB (8%) | 0 |

About 60% of each namespace is BuildKit build-cache snapshots that are not image layers (one sampled
namespace: 57 GB-apparent total, 15.7 GB layerdb-referenced, 22.9 GB unreferenced overlay2 dirs). Those
cannot be compared by digest and are bounded only per namespace (`docker_cache.max_bytes` = 40 GiB each),
so the worst case is `workers x principals x 40 GiB`, not a per-host cap.

## What Changes

- Add a per-host aggregate bound over all `docker_cache` namespaces of a host (new `docker_cache.host_max_bytes`,
  enforced at attempt begin by evicting least-recently-used whole namespaces that are not locked).
- Seed identical base images once into a host-shared read-only image store and mount it as an additional
  image store for every slot (`additionalimagestores` in each slot's `storage.conf`), so base layers are
  stored once per host. This is a measured-then-gated step, not a flag day.
- Rejected for now: `fuse-overlayfs` shared layer store (performance cost on build-heavy workloads),
  deduplicating overlay2 directories by hardlink (breaks overlayfs whiteout and upper/lower assumptions).

## Impact

zz-joe: aggregate cap removes the unbounded term; shared base-image store saves up to the redundant
35.6 GB (21.2 GB of it across workers). zz-whitby: nothing to gain today (2.5 GB, single populated worker).
Docker data-roots are NOT touched by this change until the tasks below are approved per host.
