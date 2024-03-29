import {
  CallContext,
  Metadata,
  createChannel,
  createClientFactory,
  Status,
  ClientError,
} from "nice-grpc";
import {
  DBServiceDefinition,
  StreamServiceDefinition,
  QueueServiceDefinition,
  CacapcityServiceDefinition,
} from "@livestack/vault-interface";
import {
  RetryOptions,
  retryMiddleware,
} from "nice-grpc-client-middleware-retry";

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
      getJobDatapoints: genAuthorizedGRPCFn(
        authToken,
        dbClient.getJobDatapoints.bind(dbClient)
      ).bind(dbClient),
      ensureJobAndStatusAndConnectorRecs: genAuthorizedGRPCFn(
        authToken,
        dbClient.ensureJobAndStatusAndConnectorRecs.bind(dbClient)
      ).bind(dbClient),
      updateJobInstantiatedGraph: genAuthorizedGRPCFn(
        authToken,
        dbClient.updateJobInstantiatedGraph.bind(dbClient)
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

type RetryExt = RetryOptions;

function genAuthorizedGRPCFn<REQ extends object, R>(
  authToken: string,
  fn: (req: REQ, ctx?: Partial<CallContext> & RetryExt) => R,
  opts?: { retry: boolean }
) {
  const { retry } = { retry: true, ...opts };
  return (req: REQ, ctx?: Partial<CallContext> & RetryExt): R => {
    const metadata = ctx?.metadata || new Metadata();
    metadata.set("authorization", "Bearer " + authToken);

    return fn(req, {
      ...(ctx || ({} as Partial<CallContext> & RetryExt)),
      metadata,
      ...(retry
        ? {
            // not needed if the method is marked as idempotent in Protobuf
            retry: true,
            // defaults to 1
            retryMaxAttempts: 5,
            // defaults to [UNKNOWN, INTERNAL, UNAVAILABLE, CANCELLED]
            retryableStatuses: [Status.UNAVAILABLE],
            onRetryableError(
              error: ClientError,
              attempt: number,
              delayMs: number
            ) {
              console.error(
                error,
                `Call failed (${attempt}), retrying in ${delayMs}ms`
              );
            },
          }
        : {}),
    });
  };
}
