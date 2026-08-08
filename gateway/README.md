## @livestack/gateway

Websocket gateway server for Livestack.

`initJobBinding` attaches a socket.io server to an existing HTTP server (default path `/livestack.socket.io`). Each connection is handled by a `LiveGatewayConn`, which implements the command protocol shared with `@livestack/shared`:

- **Bind**: request and bind to a job (`REQUEST_AND_BIND_CMD`).
- **Feed**: send input to a bound job (`CMD_FEED`).
- **Stream**: subscribe/unsubscribe to output streams (`CMD_SUB_TO_STREAM` / `CMD_UNSUB_TO_STREAM`), with optional history replay (`lastN` or all).
- **Unbind**: release a job binding (`CMD_UNBIND`); when the last connection to a job disconnects, the job is terminated automatically.

Authentication is optional. When `authToken` is provided, each connection must present it via socket.io `auth.token` or an `Authorization: Bearer` header; the token is matched directly or verified as a JWT against `JWT_SECRET`.

Known limitation: input-side stream subscription is not yet supported (only job input feeds and output stream subscriptions).
