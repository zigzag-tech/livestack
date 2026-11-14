## Keys

The generation `defaults` JSON keeps `handler -> digest` for the primary and adds `handler@platform/arch -> digest`
for other platforms. Every reference query already walks `json_each(g.defaults)` values, so variant digests are
protected from retention and eviction with no query change. `@` cannot appear in a handler id the registry accepts.

Activation decides the key from the candidate's manifest: if a primary exists and its manifest platform/architecture
differ from the candidate's, the key is the variant key; otherwise the primary key. One activation still makes one
generation and one receipt (`previous_digest` is the previous value under that key).

## Jobs

At acceptance a default-selection job resolves the primary exactly as before and additionally every variant of the
same handler in the same generation into `spec.handler_release_variants` (same descriptor shape plus `platform`,
`architecture`). The job therefore holds its accepted set; activation after acceptance changes nothing for it.

## Placement and attempts

`_release_compatible` returns the matching descriptor (primary first). The attempt row stores that descriptor (the
column already exists); the assignment already prefers the attempt's descriptor; result identity is checked against
the attempt's descriptor when present, the job's otherwise.

## Workers

`desired()` adds variant descriptors for supported handlers to `releases`; `defaults` stays plain handler ids. A new
worker, after installing what matches its platform, replaces a skipped default with the variant descriptor for its
own platform. An older worker skips variant descriptors as `platform_not_supported`, which is what it does today.
