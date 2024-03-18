import {
  CallContext,
  Metadata,
  createChannel,
  createClientFactory,
} from "nice-grpc";
import {
  DBServiceDefinition,
  StreamServiceDefinition,
  QueueServiceDefinition,
  CacapcityServiceDefinition,
} from "@livestack/vault-interface";
import { retryMiddleware } from "nice-grpc-client-middleware-retry";

export const genAuthorizedVaultClient = (authToken: string) =>
  findSuitableVaultServer(authToken);

export type AuthorizedGRPCClient = ReturnType<typeof findSuitableVaultServer>;

export function findSuitableVaultServer(authToken: string) {
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
  const dbClient = clientFactory.create(
    DBServiceDefinition,
    createChannel(vaultServerURL)
  );
  const queueClient = clientFactory.create(
    QueueServiceDefinition,
    createChannel(vaultServerURL)
  );
  return {
    db: {
      addDatapoint: genAuthorizedGRPCFn(
        authToken,
        dbClient.addDatapoint.bind(dbClient)
      ).bind(dbClient),
      getJobDatapoints: genAuthorizedGRPCFn(
        authToken,
        dbClient.getJobDatapoints.bind(dbClient)
      ).bind(dbClient),
      ensureJobAndStatusAndConnectorRecs: genAuthorizedGRPCFn(
        authToken,
        dbClient.ensureJobAndStatusAndConnectorRecs.bind(dbClient)
      ).bind(dbClient),
      getParentJobRec: genAuthorizedGRPCFn(
        authToken,
        dbClient.getParentJobRec.bind(dbClient)
      ).bind(dbClient),
      appendJobStatusRec: genAuthorizedGRPCFn(
        authToken,
        dbClient.appendJobStatusRec.bind(dbClient)
      ).bind(dbClient),
      getJobStreamConnectorRecs: genAuthorizedGRPCFn(
        authToken,
        dbClient.getJobStreamConnectorRecs.bind(dbClient)
      ).bind(dbClient),
      ensureStreamRec: genAuthorizedGRPCFn(
        authToken,
        dbClient.ensureStreamRec.bind(dbClient)
      ).bind(dbClient),
    },
    stream: clientFactory.create(
      StreamServiceDefinition,
      createChannel(vaultServerURL)
    ),
    queue: {
      addJob: genAuthorizedGRPCFn(
        authToken,
        queueClient.addJob.bind(queueClient)
      ).bind(queueClient),
      initInstance: genAuthorizedGRPCFn(
        authToken,
        queueClient.initInstance.bind(queueClient)
      ).bind(queueClient),
      reportAsWorker: genAuthorizedGRPCFn(
        authToken,
        queueClient.reportAsWorker.bind(queueClient)
      ).bind(queueClient),
    },
    capacity: clientFactory.create(
      CacapcityServiceDefinition,
      createChannel(vaultServerURL)
    ),
  };
}

function genAuthorizedGRPCFn<REQ extends object, CallContextExt extends {}, R>(
  authToken: string,
  fn: (req: REQ, ctx?: CallContext & CallContextExt) => R
) {
  return (req: REQ, ctx?: CallContext & CallContextExt): R => {
    const metadata = ctx?.metadata || new Metadata();
    metadata.set("authorization", "Bearer " + authToken);

    return fn(req, {
      ...(ctx || ({} as CallContext & CallContextExt)),
      metadata,
    });
  };
}
