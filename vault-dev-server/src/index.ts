import { createServer } from "nice-grpc";
import { dbService } from "./db/service";
import { getQueueService } from "./queue/service";
import { getStreamService } from "./stream/service";
import { getCapacityManager } from "./capacity/manager";
import { db } from "./db/knexConn";
import {
  CacapcityServiceDefinition,
  DBServiceDefinition,
  QueueServiceDefinition,
  StreamServiceDefinition,
} from "@livestack/vault-interface";
import { startBootstrapNode } from "./storage-p2p/startBootstrapNode";

export async function launchVaultDevServer(port?: string | number) {
  // if (!port) {
  //   const { default: getPort } = await import("get-port");
  //   port = await getPort({ port: 50508 });
  // }
  const server = createServer();
  server.add(DBServiceDefinition, dbService(db));
  server.add(QueueServiceDefinition, getQueueService());
  server.add(StreamServiceDefinition, getStreamService(db));
  server.add(CacapcityServiceDefinition, getCapacityManager());

  const HOST = process.env.VAULT_SERVER_LOCAL_DEV_SERVER_HOST || "0.0.0.0";
  const PORT = Number(process.env.VAULT_SERVER_LOCAL_DEV_SERVER_PORT) || port;
  const address = `${HOST}:${PORT}`;

  await startBootstrapNode();

  await server.listen(address);
  console.info(`🌌🔒 Vault dev server started. Listening on ${address}.`);
  return { port };
}

if (require.main === module) {
  launchVaultDevServer();
}
