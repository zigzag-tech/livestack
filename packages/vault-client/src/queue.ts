import { createChannel, createClient } from "nice-grpc";
import { QueueServiceDefinition } from "@livestack/vault-interface";

export const queueClient = createClient(
  QueueServiceDefinition,
  createChannel("livedev.example.net:50504")
);
