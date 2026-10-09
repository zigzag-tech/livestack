## Ownership of durable state

The store (`<path>/<principal-namespace>/entries/<key>/{data,meta.json}`) is owned by the worker process alone: it
writes at store time and reads/deletes at restore time, under the namespace flock. The handler and everything it
launches see only ordinary directories inside the attempt (the restored tree, the request/response files, the commit
file). Keys and digests cross the boundary in `response.json` (worker to handler) and in the handler's receipt.

## Decisions

- **Content digest, not shape.** `tree_digest` hashes every regular file (sha256) and every link
  target, with kind, relative path, executable bit and size, ignoring mtimes. It is stored in
  `meta.json` and recomputed on the *copy* that is about to be used, so what the handler
  gets is what was stored. Cost is one sequential read of the tree per restore and two per
  store (source, then the copy); measured on a 1.8 GB Rust target subset in the benchday
  change `release-build-cache`.
- **Pre-digest entries are never trusted.** An entry without a valid digest is dropped and the
  attempt runs cold. One cold run per entry after the roll; chosen over a grandfather rule
  because a rule that trusts old entries is exactly the hole this closes.
- **Writer isolation.** The worker already holds the only legitimate write path (restore and
  store run in the worker process after/before the attempt, outside the sandbox). The attempt
  unit now also gets `InaccessiblePaths=<store>`, verified with systemd in a `--user` manager with
  `PrivateTmp`. Residual: other processes of the same uid that are not inside such a sandbox
  (another worker's handler, a human shell) can still write the store; digests and audits catch
  accidents and make tampering detectable, they do not authenticate against a same-uid adversary.
  Giving each worker class its own uid is the real fix and is out of scope.
- **Success-only store.** The worker already gated the store on the handler's commit marker; a
  build script in a failed attempt could forge that marker. The store is now also gated on the
  attempt's own verdict, which the worker computes from the exit receipt.
- **Audit is the handler's comparison, scheduled by the worker.** Only the handler knows what "the same tree" means (for a
  compiler: every compiled artifact). The worker chooses *which* attempts audit so the schedule cannot be skewed by the
  handler, and returns the key and digest so the handler's receipt names exactly what was served. An audit attempt builds
  cold and compares trees instead of building twice: one build, not two, and the check is on the stored bytes themselves.
- **Replace on named mismatch.** The handler that rebuilt cold after an audit mismatch lists the
  path in `replace`; the worker stores the cold-built tree over the entry. Without that, a bad entry
  would be served until `refresh_every` reached it.
- **Not changed:** key construction (the handler can add any identity it needs, such as the
  compiler version, by writing a key file that the manifest names in `key_paths`), the bound
  (`max_bytes` per namespace, LRU), flock discipline.
