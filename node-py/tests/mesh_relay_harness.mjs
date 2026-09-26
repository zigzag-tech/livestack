// The Python lane's test relay: the REAL mesh_relay engine, driven over
// JSON-lines on stdin/stdout.
//
// test_mesh_peer.py / test_mixed_roster.py must not imitate the relay, the
// mux, the capability check or the quota (rule 11) — so this harness hosts
// the real `createRelayServer` from packages/mesh_relay, configured for the
// livestack realm's cosmetics, with one registered `daemon_ws` route (the
// tunnel case the engine serves itself, without a handler's involvement).
//
// All private keys stay in Python: the harness receives only the realm's
// attachment trust public key and the cap key ring's verify keys, both as
// boot config on argv. Python mints bdrt1 attachments (relay_control) and
// bdsr1 caller caps; the relay verifies them with its own code — the same
// two-halves-must-agree discipline as test_relay_control.py.
//
// Protocol: harness prints one {"ready": ...} line; Python then sends one
// JSON command per line and reads one response line per command.
//   {cmd: "set_quota", maxStreamsPerAccount: n}
//   {cmd: "relay_down"} / {cmd: "relay_up"}   (restart drill, same port)
//   {cmd: "stop"}

import { createRequire } from 'node:module';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const MESHLINK_REPO = process.env.MESHLINK_REPO
  ? resolve(process.env.MESHLINK_REPO)
  : resolve(dirname(fileURLToPath(import.meta.url)), '../../../../meshlink');

// 'ws' resolves from the relay package's own node_modules, no matter where
// this harness file lives.
const require = createRequire(pathToFileURL(resolve(MESHLINK_REPO, 'packages/mesh_relay/package.json')));
const { WebSocketServer } = require('ws');

const { createRelayServer } = await import(pathToFileURL(resolve(MESHLINK_REPO, 'packages/mesh_relay/src/server.ts')).href);
const { RelayTunnels } = await import(pathToFileURL(resolve(MESHLINK_REPO, 'packages/mesh_relay/src/tunnels.ts')).href);
const { RelayQuota } = await import(pathToFileURL(resolve(MESHLINK_REPO, 'packages/mesh_relay/src/quota.ts')).href);

const cfg = JSON.parse(process.argv[2]);

// The relay engine logs auth refusals with console.log — reroute to stderr:
// stdout is the JSON-lines command protocol and a stray log line corrupts it.
console.log = (...args) => console.error(...args);

// A dedicated env object so "set_quota" reaches the RelayQuota's live reads
// without touching the process environment.
const quotaEnv = {};
const quota = new RelayQuota({ env: quotaEnv });

// Multi-realm constructor form: the realm's attachment trust keys AND its
// cosmetic audience (DR-4) — the single-realm form would expect the relay
// package's default (benchday) audience and refuse livestack tokens.
const tunnels = new RelayTunnels({
  realm: cfg.realm,
  relayId: cfg.relayId,
  keysForRealm: (realm) => (realm === cfg.realm ? [cfg.hubPub] : []),
  attachmentAudienceForRealm: (realm) =>
    realm === cfg.realm ? cfg.attachmentAudience : undefined,
});

// Realm-aware door authorization, mirroring meshlink's realm_door_e2e
// buildRealmEngine: every presented realm key authorizes, this relay process
// answers for the livestack realm, and each realm's configured typ/aud is
// what its caps must carry — a cap wearing the package default's (benchday)
// cosmetics against the livestack realm is refused. No `keyRing`: the realm
// keys alone authorize the door (relay chaining would need keyRing alongside).
const BENCHDAY_CLAIMS = {
  typ: 'benchday-speech-relay-capability',
  aud: 'benchday-speech-relay',
};

const engine = createRelayServer(
  {
    relayId: cfg.relayId,
    keyRing: null,
    quota,
    tunnels,
    maxBodyBytes: 8 * 1024 * 1024,
    maxInflightBytes: 32 * 1024 * 1024,
    upstreamRelayIds: new Map(),
    inventory: () => ({}),
    routePrefix: cfg.routePrefix,
    doorPath: cfg.doorPath,
    realmKeys: cfg.realmCapKeys,
    targetRealm: cfg.realm,
    legacyRealm: 'benchday',
    claimsForRealm: (realm) =>
      realm === cfg.realm
        ? { typ: cfg.capabilityType, aud: cfg.capabilityAudience }
        : BENCHDAY_CLAIMS,
  },
  {
    routes: [{ pattern: `/${cfg.route}/:target`, kind: 'daemon_ws', transport: 'ws' }],
    http: [],
    ws: [{
      kind: 'daemon_ws',
      scope: 'terminal.proxy',
      targets: () => new Map(),
      // Reached only when the engine has no attached tunnel for the target:
      // a caller asking for a daemon that is not attached. Named, not silent.
      serve: (ctx) => ctx.refuse(404, 'daemon_not_attached'),
    }],
  },
);

const HOST = '127.0.0.1';
// Track every accepted socket ourselves: closeAllConnections does not destroy
// upgraded (WebSocket) connections, and the attached target's tunnel is one —
// a restart that leaves the tunnel alive is not a restart.
const sockets = new Set();
engine.server.on('connection', (s) => {
  sockets.add(s);
  s.on('close', () => sockets.delete(s));
});
await new Promise((res, rej) => {
  engine.server.once('error', rej);
  engine.server.listen(cfg.port ?? 0, HOST, () => res());
});
const port = engine.server.address().port;
const relayUrl = `ws://${HOST}:${port}`;

function reply(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

reply({ ready: true, port, relayUrl, routePrefix: cfg.routePrefix, doorPath: cfg.doorPath });

process.stdin.on('data', (chunk) => {
  for (const line of chunk.toString().split('\n')) {
    if (!line.trim()) continue;
    const cmd = JSON.parse(line);
    if (cmd.cmd === 'set_quota') {
      if (cmd.maxStreamsPerAccount) quotaEnv.BENCHDAY_RELAY_MAX_STREAMS_PER_ACCOUNT = String(cmd.maxStreamsPerAccount);
      else delete quotaEnv.BENCHDAY_RELAY_MAX_STREAMS_PER_ACCOUNT;
      reply({ ok: true });
    } else if (cmd.cmd === 'relay_down') {
      // server.close() waits for connections to end; the attached target's
      // tunnel is long-lived, so force-destroy the tracked sockets after a
      // beat — that is what makes this a REAL restart for the attached target.
      engine.server.close(() => reply({ ok: true }));
      setTimeout(() => { for (const s of sockets) s.destroy(); }, 300);
    } else if (cmd.cmd === 'relay_up') {
      engine.server.listen(port, HOST, () => reply({ ok: true, port }));
    } else if (cmd.cmd === 'stop') {
      engine.server.close(() => process.exit(0));
      setTimeout(() => process.exit(0), 2000).unref();
    } else {
      reply({ ok: false, error: 'unknown cmd ' + cmd.cmd });
    }
  }
});
process.stdin.on('end', () => process.exit(0));
