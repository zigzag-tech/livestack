## Why the manifest is declarative

The coordinator must decide "did the image inputs change" without running app
code, so inputs are listed paths (relative, distinct, no `..`). How the image is
built stays in the app's handler: the build must be byte-for-byte what a real job
runs or the layers will not be hit. The manifest only names that handler.

## Why not a worker-side `docker build`

A generic `docker build` job would have to reproduce the app's build args,
no-cache filters, base-image pins and context preparation. Those live in the
handler already; duplicating them in a manifest is how a warm job silently warms
the wrong layers.

## Idle-time guarantee

Harmony has no preemption and priority 0 is its lowest, so "does not block real
jobs" is carried by submission: warm only to idle eligible workers and never
while the app has queued work. Worst case a real job queues behind one cold build
on one worker while the others stay free.

## Hit verification

Each warm result records `warmed|skipped`, reason and elapsed time; a following
real job's `docker_cache` outcome (`hit`) and the app's own "built in N s" line
show the effect. A warm that builds different layers than the real job shows as
no change in that line and is a defect of the app's handler, not of the mechanism.
