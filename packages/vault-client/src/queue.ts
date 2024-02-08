import { createChannel, createClient } from "nice-grpc";
import { QueueServiceDefinition } from "@livestack/vault-interface";

export const queueClient = createClient(
  QueueServiceDefinition,
  createChannel("livedev.zztech.io:50504")
);
