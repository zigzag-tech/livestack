## 1. Measure (done 2026-11-06)
- [x] 1.1 Layer overlap via layerdb diff-ids on zz-joe and zz-whitby (numbers in proposal.md)
- [x] 1.2 Dependency-cache duplicates: see `node-py/livestack_node/workloads/cache_dedupe.py`

## 2. Host aggregate bound
- [ ] 2.1 `docker_cache.host_max_bytes` validated in `settings()`; named outcome `host-cap-evicted`
- [ ] 2.2 Test: three fake namespaces over the cap evict the oldest unlocked one, never a locked or the current one
- [ ] 2.3 Roll out zz-joe at 300 GiB (current 349 GB), then zz-whitby

## 3. Shared base-image store
- [ ] 3.1 Seed script and atomic switch; storage.conf `additionalimagestores`
- [ ] 3.2 Canary cold+warm on one slot; measure slot bytes before and after
- [ ] 3.3 Enable per slot after 3.2 passes; document rollback
