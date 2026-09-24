"""The scheduler's plans are byte-identical to the corpus recorded before the
scheduler-policy-routine refactor (design §3, task 1.1). A failure here means
`schedule()` changed behaviour; the first differing state is printed."""
from policy_golden.generate import N_STATES, lines, read_golden


def test_schedule_matches_the_pre_refactor_golden_corpus():
    golden = read_golden()
    assert len(golden) == N_STATES
    now = lines()
    for i, (want, got) in enumerate(zip(golden, now)):
        assert got == want, f"state {i} differs:\n want {want}\n  got {got}"


def test_corpus_exercises_every_action_and_reason():
    """The corpus is only proof if it reaches the branches the refactor moves."""
    import json
    seen = set()
    for line in read_golden():
        for kind, _job, _target, reason in json.loads(line)["actions"]:
            seen.add((kind, reason.split(":")[0]))
    for want in [("Admit", "run on existing LOCAL"), ("Admit", "run on existing SPOT"),
                 ("Admit", "run on existing LAST_RESORT"),
                 ("Provision", "burst SPOT"), ("Provision", "burst LAST_RESORT"),
                 ("Queue", "no feasible target meets the deadline now"),
                 ("Deprovision", "idle burst worker, no demand")]:
        assert want in seen, want
