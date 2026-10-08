"""Translate workload placements and completions into bounded Fleet ledger rows."""
from ..ledger import Candidate, Decision, new_decision_id


def admission_record(decision, *, now, emitter_id, kind, owner, selector, locality_host,
                    job_id, attempt_id, environment_handle, environment_generation,
                    filtered_candidates=()):
    """Build the audit row from the exact scheduler snapshot used for admission."""
    policy_rows = {row['id']: row for row in decision['rows']}
    policy_candidates = decision['candidates']
    positions = {row['id']: i for i, row in enumerate(decision['rows'])}
    eligible = sorted(
        (row for row in decision['rows'] if row['eligible']),
        key=lambda row: (row['score'], positions[row['id']]))
    ranks = {row['id']: i for i, row in enumerate(eligible, 1)}
    candidates = []
    seen = set()
    for candidate in policy_candidates:
        candidate_id = candidate['id']
        seen.add(candidate_id)
        row = policy_rows[candidate_id]
        features = candidate.get('features') or {}
        chosen = candidate_id == decision.get('chosen')
        candidates.append(Candidate(
            id=candidate_id,
            host_id=features.get('host_id'),
            state='fresh',
            ready=True,
            outcome='chosen' if chosen else ('ranked' if row['eligible'] else 'filtered'),
            reason=row.get('reason') or ('chosen by scheduler' if chosen else 'scheduler filtered candidate'),
            rank=ranks.get(candidate_id),
            inputs_at=now,
            detail={**features, 'score': row.get('score'), 'eligible': row['eligible']},
        ))
    for candidate in filtered_candidates:
        candidate_id = candidate['worker']
        if candidate_id in seen:
            continue
        candidates.append(Candidate(
            id=candidate_id,
            host_id=candidate.get('host'),
            state='fresh',
            ready=True,
            outcome='filtered',
            reason='filtered: ' + candidate['reason'],
            inputs_at=now,
            detail={'placement_filter': candidate['reason']},
        ))
        seen.add(candidate_id)

    winner = policy_rows.get(decision.get('chosen'))
    request = {
        'owner': owner,
        'sla': 'batch',
        'selector': selector,
        'locality_host': locality_host,
        'job_id': job_id,
        'attempt_id': attempt_id,
        'environment_handle': environment_handle,
        'environment_generation': environment_generation,
    }
    return Decision(
        emitter='job-caller', emitter_id=emitter_id, decision='admit',
        candidates=candidates, kind=kind, request=request,
        chosen=decision.get('chosen'),
        reason=(winner or {}).get('reason') or 'selected by the workload scheduler',
        dispatched=True, decision_id=decision['decision_id'], ts=now,
    )


def completion_record(*, now, emitter_id, decision_id, kind, owner, job_id, attempt_id,
                      worker, environment_handle, environment_generation, attempt_seconds,
                      product_outcome, job_state, environment_receipt=None, cause_kind=None):
    """Build a linked completion event; it does not change the original decision."""
    receipt = environment_receipt or {}
    return Decision(
        emitter='job-caller', emitter_id=emitter_id, decision='admit',
        kind=kind,
        request={
            'owner': owner,
            'job_id': job_id,
            'attempt_id': attempt_id,
            'environment_handle': environment_handle,
            'environment_generation': environment_generation,
        },
        chosen=worker,
        reason='workload attempt completed',
        parent_decision_id=decision_id,
        dispatched=True,
        outcome={
            'status': 'ok' if product_outcome == 'succeeded' else 'failed',
            'latency_ms': max(0.0, attempt_seconds * 1000.0),
            'served_by': worker,
            'recorded_at': now,
            'job_id': job_id,
            'attempt_id': attempt_id,
            'environment_handle': environment_handle,
            'environment_generation': environment_generation,
            'environment_reuse_outcome': receipt.get('reuse_outcome'),
            'product_outcome': product_outcome,
            'job_state': job_state,
            'cause_kind': cause_kind,
        },
        decision_id=new_decision_id(now), ts=now,
    )
