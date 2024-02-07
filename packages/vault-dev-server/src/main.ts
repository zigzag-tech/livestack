import { createServer } from "nice-grpc";
import { jobDbService } from "./db/jobs";
import { db } from "./db/knexConn";
import { JobDBServiceDefinition } from "@livestack/vault-interface";

async function main() {
  const server = createServer();
  server.add(JobDBServiceDefinition, jobDbService(db));
  const HOST = process.env.HOST || "0.0.0.0";
  const PORT = Number(process.env.PORT) || 50051;
  const address = `${HOST}:${PORT}`;

  await server.listen(address);
  console.log(`Vault dev server listening on ${address}.`);
}

main();
