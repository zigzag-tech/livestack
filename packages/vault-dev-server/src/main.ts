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

async function main() {
  const server = createServer();
  server.add(DBServiceDefinition, dbService(db));
  server.add(QueueServiceDefinition, getQueueService());
  server.add(StreamServiceDefinition, getStreamService());

  const HOST = process.env.HOST || "0.0.0.0";
  const PORT = Number(process.env.PORT) || 50508;
  const address = `${HOST}:${PORT}`;

  await server.listen(address);
  console.info(`🌌🔒 Vault dev server started. Listening on ${address}.`);
}

main();
