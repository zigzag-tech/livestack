- [x] 1. Synchronize extraction schema and semantic checks; verify contract tests and byte parity. Ledger: no inference admitted by validation.
- [x] 2. Resolve extraction profile and reject unqualified admission and retired adapters; verify refusal before model calls. Ledger: named refusal only.
- [ ] 3. Implement bounded nominal-27B extraction adapter and authenticated facade integration; verify real HTTP, deadline, grounding, identity and outage controls. Ledger: existing inference/admission outcomes.
- [ ] 4. Produce genuine qualification evidence and matching registry metadata; refuse unavailable evidence rather than fabricate it. Ledger: qualification receipt and measured profile identity.
- [ ] 5. Land, obtain owner-operated guarded publish and verify live typed extraction; archive this worker change after proof. Ledger: deployment and readback evidence.

Verification: 19 JSON contract files are byte-identical to the consumer copy.
The additive contract revision is v1.1.0; HASH binds sorted relative JSON paths,
NUL delimiters and exact bytes (recipe in HASH.md). Contract/ingress controls
use a task-private HTTPX 0.27.2 environment compatible with the installed
Starlette test client. These controls do not constitute model qualification.
The foundation's 34 contract/ingress checks pass, including the content receipt,
authenticated refusal before admission and both retired-adapter refusal paths.
Consumer contract and extraction-client controls pass (9 checks).
