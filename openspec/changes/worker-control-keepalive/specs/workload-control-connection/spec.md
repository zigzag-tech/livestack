## ADDED Requirements

### Requirement: Worker control traffic reuses kept-alive connections
A workload worker SHALL send its control requests (claim, report, complete,
verify-compilation) over one persistent HTTP/1.1 connection, and its lease
renewals over a second persistent connection dedicated to the lease keeper, so
that an established attempt renews without opening new TCP connections.
Object and CAS transfers SHALL NOT use these connections.

#### Scenario: Many renewals ride one connection
- **WHEN** a lease keeper renews N times against an authority that keeps connections alive
- **THEN** the authority accepts exactly one connection for those renewals

#### Scenario: Renewal while new connects are blocked
- **WHEN** after the lease keeper's first grant every new TCP connect to the authority hangs
- **THEN** subsequent renewals still succeed within the lease

### Requirement: A dropped control connection is replaced
A worker SHALL retry a control request once on a fresh connection when it
fails on a reused connection before any response byte arrives, and SHALL NOT
retry any other failure at the connection layer.

#### Scenario: Authority closes an idle connection
- **WHEN** the authority closes the kept connection between two renewals
- **THEN** the next renewal succeeds on a new connection

### Requirement: The authority keeps control connections alive within bounds
The workload authority SHALL send `Content-Length` on every JSON response,
SHALL keep the connection open only when the request body was fully consumed
and the client did not ask to close, SHALL close a kept connection idle for
its socket timeout (15 s), and SHALL bound open connections at
32 + 2 x worker principals. At the bound it SHALL close the longest-idle kept
connection to admit a new one, and SHALL drop a new connection (with a named
log line) only when every open connection is busy.

#### Scenario: Refused request with an unread body
- **WHEN** a POST is refused before its body is read
- **THEN** the response carries `Content-Length` and `Connection: close`

#### Scenario: Idle workers hold every slot
- **WHEN** kept-alive worker connections hold every slot and a verifier or object request opens a new connection
- **THEN** the longest-idle kept connection is closed, the new request succeeds, and the evicted worker's next request succeeds on a new connection

#### Scenario: Every slot busy
- **WHEN** every open connection is serving a request and another arrives
- **THEN** the new connection is closed without a response and the authority logs `workload_connection_dropped_at_bound`
