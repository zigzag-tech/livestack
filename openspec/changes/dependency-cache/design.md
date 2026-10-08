## Decisions

- **Declaration lives in the job's source, not worker config**: the app owns what it
  installs and what determines it; the worker owns where, how much and for whom. The
  manifest is read from the unpacked, hash-verified source and is closed-schema; paths
  are canonical relative, never absolute or `..`; nothing it names is ever executed.
- **Key = content of declared files**, plus namespace (submitting principal, as in
  `docker_cache`), epoch, handler executable identity (realpath, size, mtime) and
  platform/machine/libc, because installed trees hold compiled addons. A missing or
  unsafe key path means "not cacheable", never a partial key.
- **Copy, never link**: a restored tree is a real copy (`cp -a --reflink=auto`), so no
  step of the attempt can write the store through a shared inode.
- **Commit marker written by the handler**: the worker cannot know an install finished;
  a failed or interrupted `npm ci` leaves no marker and nothing is stored. The key is
  recomputed at store time and must equal the restore-time key (a run that rewrote its
  lockfile stores nothing).
- **Verified on both ends**: stores refuse absolute symlinks, symlinks that leave the
  source, special files and unreadable subtrees; restores re-scan and compare file count
  and allocated bytes with the entry's meta, dropping a damaged entry.
- **Drift bound**: `refresh_every` N makes every Nth attempt (hash of attempt id) skip
  the restore, install cold and replace the entry, bounding any undetected same-size
  corruption to N attempts.
- **Concurrency**: shared flock for restore, exclusive non-blocking flock for store and
  evict; busy means a named miss, never a wait.
- **Not covered**: build outputs (keying on source changes every train), the docker
  data root (docker_cache), macOS/Windows workers, task-environment attempts (own
  mechanism).
- **Restore on request, not at start**: the transported source is not the handler's working tree until the
  handler materialises it (Benchday's archive layout is app/ plus sibling groups, and its normaliser refuses
  any extra file). The handler writes a request naming its root; the worker serves it from its poll loop,
  restricted to the attempt's source directory, and answers once. Found when every real attempt reported
  `no-manifest`: the manifest lived at app/.livestack/, not source/.livestack/.
