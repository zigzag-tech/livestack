## Decisions

**Declared on the handler, in operator config.** Release handlers are submitted by the zzops release service, e2e by the hub train, warm-up by the image-warm service. Declaring the class where the handler is authorized (`limits`, beside `describe_required_handlers` and `scope_required_handlers`) needs no submitter change and cannot be forgotten by a new one. The job-level `priority` stays and combines by `max`, so an urgent e2e job can still outrank a default one. No handler id or host is named in code.

**The hold is the existing hold.** `_hold_for_starved` charged a starved job's `admit` to each host that refuses it for lack of resources it could ever supply, so jobs placed later in the round see the host as already admitted. That machinery is split into `_hold_plan` + `_hold_charge` and reused. A priority hold differs only in when it binds: the job's plan is queued (`pending_holds`) and charged when the loop reaches a job of strictly lower effective priority. The queue is sorted by effective priority, so everything after that point is no higher, and equal-priority jobs keep today's behaviour (a small job may pass a large young one).

**Holds are per round.** Nothing persists; the next round recomputes from the queue. A starved job's immediate hold is unchanged, and starved jobs are ordered first regardless of class, so aging still lets e2e through.

**Cause.** After the per-worker refusals, a refusal with a resource code on a held host is relabelled `held_for_higher_priority` only when the job would have fit that host had no hold been charged (`before_holds` snapshot). A job that did not fit anyway keeps its plain resource code. The held job itself reports its plain resource refusal.

**Not done.** Preemption (a running attempt is never touched). A priority on the roster ordering (`roster.py`); its reason column already carries the typed blockers, so the hold shows there.
