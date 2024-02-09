import { createServer } from "nice-grpc";
import { dbService } from "./db/service";
import { getQueueService } from "./queue/service";
import { getStreamService } from "./stream/service";
import { db } from "./db/knexConn";
import {
  DBServiceDefinition,
  QueueServiceDefinition,
  StreamServiceDefinition,
} from "@livestack/vault-interface";

export async function launchVaultDevServer(port = 50508) {
  const server = createServer();
  server.add(DBServiceDefinition, dbService(db));
  server.add(QueueServiceDefinition, getQueueService());
  server.add(StreamServiceDefinition, getStreamService());

  const HOST = process.env.VAULT_SERVER_LOCAL_DEV_SERVER_HOST || "0.0.0.0";
  const PORT = Number(process.env.VAULT_SERVER_LOCAL_DEV_SERVER_PORT) || port;
  const address = `${HOST}:${PORT}`;

  await server.listen(address);
  console.info(`🌌🔒 Vault dev server started. Listening on ${address}.`);
}
