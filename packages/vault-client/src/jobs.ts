import { createChannel, createClient } from "nice-grpc";
import { DBServiceDefinition } from "@livestack/vault-interface";
export const jobDBClient = createClient(
  DBServiceDefinition,
  createChannel("localhost:50051")
);
