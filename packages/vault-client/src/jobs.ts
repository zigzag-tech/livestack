import { createChannel, createClient } from "nice-grpc";
import { JobDBServiceDefinition } from "@livestack/vault-interface";
export const jobDBClient = createClient(
  JobDBServiceDefinition,
  createChannel("localhost:50051")
);
