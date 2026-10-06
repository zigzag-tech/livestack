## ADDED Requirements

### Requirement: A public upload address is per principal
A principal MAY carry `upload_base_url`; grants it mints SHALL use that origin, and other principals' grants SHALL be unaffected.

#### Scenario: Two principals mint grants
- **WHEN** one principal has `upload_base_url` and another does not
- **THEN** only the first principal's `upload_url` uses that origin

### Requirement: A global public address cannot silently change several principals' grants
The authority SHALL refuse to start when `public_base_url` is set and more than one principal may mint grants, naming the principals.
