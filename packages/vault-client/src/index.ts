import { createChannel, createClient } from "nice-grpc";
import {
  DBServiceDefinition,
  StreamServiceDefinition,
  QueueServiceDefinition,
} from "@livestack/vault-interface";

export const vaultClient = findSuitableVaultServer();

export function findSuitableVaultServer() {
  let vaultServerURL: string;
  if (process.env.LIVESTACK_VALULT_SERVER_URL) {
    vaultServerURL = process.env.LIVESTACK_VALULT_SERVER_URL;
  } else if (process.env.LAUNCH_LOCAL_VAULT_DEV_SERVER) {
    try {
      require.resolve("@livestack/vault-dev-server");
      const main = require("@livestack/vault-dev-server");
      const port = 50508;
      main.launchVaultDevServer(port);
      vaultServerURL = `localhost:${port}`;
    } catch (e) {
      console.log(e);
      throw new Error(
        "Vault dev server module @livestack/vault-dev-server not installed or failed to launch. Aborting app."
      );
    }
  } else {
    vaultServerURL = "livedev.zztech.io:50504";
  }
  return {
    db: createClient(DBServiceDefinition, createChannel(vaultServerURL)),
    stream: createClient(
      StreamServiceDefinition,
      createChannel(vaultServerURL)
    ),
    queue: createClient(QueueServiceDefinition, createChannel(vaultServerURL)),
  };
}
