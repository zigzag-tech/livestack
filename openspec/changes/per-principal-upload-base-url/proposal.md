## Why
`public_base_url` is server-wide, so enabling a public relay address for one principal's grants silently changed every other
grant-minting principal's `upload_url` and broke a consumer that checks the URL against the authority's own address (2026-10-06).
## What changes
- `Principal.upload_base_url` (origin only, requires `upload_grants`): the host placed in the `upload_url` of grants THAT principal mints.
- Startup refuses a global `public_base_url` together with more than one grant-minting principal, by name.
## Impact
Additive. Existing configs with one grant principal and a global value keep working.
