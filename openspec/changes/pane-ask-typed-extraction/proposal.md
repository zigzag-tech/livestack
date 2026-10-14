## Why

Benchday's approved `retire-chipgen-small-model` migration sends bounded question
extraction to the existing `pane-ask-27b-v1` profile. The worker contract rejects
the extraction question type and the LLM facade does not expose its typed route.
The generic decision plumbing described in `_plans/typed-decision-inference.md`
survives, but its historical choice/binary-only schema and retired Laya adapters
cannot implement this consumer. This change completes that prerequisite.

## What Changes

- Match the consumer's extraction envelope and validate structured answers.
- Resolve the existing profile's limits and refuse unqualified implementations.
- Add a bounded nominal-27B extraction adapter and authenticated facade route,
  preserving residency admission, deadlines, source identity and named failures.
- Verify real HTTP boundaries and qualification refusal; publish only through
  the shared service's owner. No Laya, small-model or cloud fallback is added.

## Capabilities

### New Capabilities
- `typed-decision-extraction`: qualified, source-bound question extraction.

### Modified Capabilities

None. No new logical extraction profile or generator is introduced.

## Impact

Decision schema copies, validation/admission, a separate extraction module and
the LLM facade. Benchday owns its migration and archive; this repository owns
the worker implementation and its own change artifacts.
