# Vault Dev Server

This is a dev server for the Vault service. It is a gRPC server (nice-grpc) that runs in a single process and exposes the DB, Queue, Stream, and Capacity services from `@livestack/vault-interface`. It persists to a file-based SQLite database — `./livestack.sqlite` by default, overridable via the `DB_PATH` env var.

## Prerequisites

- Node.js (no pinned version; `@types/node` is `^24`)
- Redis — reachable at `localhost:6379` by default, overridable via `REDIS_HOST` / `REDIS_PORT`

## Setup

1. Clone the repository
2. Run `yarn` at the repo root (yarn workspaces)
3. Run `npm run dev` (ts-node src/index.ts)

## Connecting

- Listens on port `50508` by default, overridable via `VAULT_SERVER_LOCAL_DEV_SERVER_PORT` (and `..._HOST`).
- Point clients at it with `LIVESTACK_VAULT_SERVER_URL=127.0.0.1:<port>`.
