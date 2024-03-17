import { createChannel, createClientFactory } from "nice-grpc";
import {
  DBServiceDefinition,
  StreamServiceDefinition,
  QueueServiceDefinition,
  CacapcityServiceDefinition,
} from "@livestack/vault-interface";
import { retryMiddleware } from "nice-grpc-client-middleware-retry";

export const vaultClient = findSuitableVaultServer();

export function findSuitableVaultServer() {
  const clientFactory = createClientFactory().use(retryMiddleware);

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
  console.info("Using vault server at:", vaultServerURL);

  return {
    db: clientFactory.create(
      DBServiceDefinition,
      createChannel(vaultServerURL)
    ),
    stream: clientFactory.create(
      StreamServiceDefinition,
      createChannel(vaultServerURL)
    ),
    queue: clientFactory.create(
      QueueServiceDefinition,
      createChannel(vaultServerURL)
    ),
    capacity: clientFactory.create(
      CacapcityServiceDefinition,
      createChannel(vaultServerURL)
    ),
  };
}
