import { createChannel, createClient } from "nice-grpc";
import { StreamServiceDefinition } from "@livestack/vault-interface";

export const streamClient = createClient(
  StreamServiceDefinition,
  createChannel("localhost:50051")
);
