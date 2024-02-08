import { createChannel, createClient } from "nice-grpc";
import { DBServiceDefinition } from "@livestack/vault-interface";

export const dbClient = createClient(
  DBServiceDefinition,
  createChannel("livedev.zztech.io:50504")
);
