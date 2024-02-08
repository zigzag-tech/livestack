import { createChannel, createClient } from "nice-grpc";
import { StreamServiceDefinition } from "@livestack/vault-interface";

export const streamClient = createClient(
  StreamServiceDefinition,
  createChannel("livedev.zztech.io:50504")
);
