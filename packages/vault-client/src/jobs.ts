import { createChannel, createClient } from "nice-grpc";
import { DBServiceDefinition } from "@livestack/vault-interface";

export const dbClient = createClient(
  DBServiceDefinition,
  createChannel("localhost:50504")
);
