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

export const genAuthorizedVaultClient = async (authToken: string) =>
  await findSuitableVaultServer(authToken);

export type GRPCVaultClient = Awaited<
  ReturnType<typeof findSuitableVaultServer>
>;

const connOpts = {
  "grpc.keepalive_time_ms": 1000 * 10,
  "grpc.keepalive_timeout_ms": 1000 * 60 * 30,
  // "grpc.max_connection_age_ms": ONE_YEAR,
  // "grpc.client_idle_timeout_ms": ONE_YEAR,
  "grpc.keepalive_permit_without_calls": 1,
};
import select, { Separator } from "@inquirer/select";
export async function findSuitableVaultServer(authToken: string) {
  const answer = await select({
    message:
      "Select a vault server option to continue \n(help: https://live.dev/DOC_TODO).",
    choices: [
      {
        name: "live.dev (recommended)",
        value: "live_dot_dev",
        description:
          "Cloud-based, user-friendly environment with live data visualization, production-ready scaling, plus more.",
      },
      {
        name: "A minimal, local vault daemon",
        value: "local",
        description:
          "Suitable for development and full local testing scenarios.",
      },
    ],
  });
  const clientFactory = createClientFactory().use(retryMiddleware);

  let vaultServerURL: string;
  if (process.env.LIVESTACK_VALULT_SERVER_URL) {
    vaultServerURL = process.env.LIVESTACK_VALULT_SERVER_URL;
  } else {
    vaultServerURL = "livedev.zztech.io:50504";
  }
  console.info("Using vault server at:", vaultServerURL);

  const dbClient = clientFactory.create(
    DBServiceDefinition,
    createChannel(vaultServerURL, undefined, connOpts)
  );
  const queueClient = clientFactory.create(
    QueueServiceDefinition,
    createChannel(vaultServerURL, undefined, connOpts)
  );

  const streamClient = clientFactory.create(
    StreamServiceDefinition,
    createChannel(vaultServerURL, undefined, connOpts)
  );

  const capacityClient = clientFactory.create(
    CacapcityServiceDefinition,
    createChannel(vaultServerURL, undefined, connOpts)
  );
  return {
    db: {
      getJobRec: genAuthorizedGRPCFn(
        authToken,
        dbClient.getJobRec.bind(dbClient)
      ).bind(dbClient),
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
    },
    stream: {
      pub: genAuthorizedGRPCFn(
        authToken,
        streamClient.pub.bind(streamClient)
      ).bind(streamClient),
      sub: genAuthorizedGRPCFn(
        authToken,
        streamClient.sub.bind(streamClient)
      ).bind(streamClient),
      valuesByReverseIndex: genAuthorizedGRPCFn(
        authToken,
        streamClient.valuesByReverseIndex.bind(streamClient)
      ).bind(streamClient),
      allValues: genAuthorizedGRPCFn(
        authToken,
        streamClient.allValues.bind(streamClient)
      ).bind(streamClient),
      lastValue: genAuthorizedGRPCFn(
        authToken,
        streamClient.lastValue.bind(streamClient)
      ).bind(streamClient),
      ensureStream: genAuthorizedGRPCFn(
        authToken,
        streamClient.ensureStream.bind(streamClient)
      ).bind(streamClient),
    },
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
    capacity: {
      reportAsInstance: genAuthorizedGRPCFn(
        authToken,
        capacityClient.reportAsInstance.bind(capacityClient)
      ).bind(capacityClient),
      respondToCapacityQuery: genAuthorizedGRPCFn(
        authToken,
        capacityClient.respondToCapacityQuery.bind(capacityClient)
      ).bind(capacityClient),
      respondToProvision: genAuthorizedGRPCFn(
        authToken,
        capacityClient.respondToProvision.bind(capacityClient)
      ).bind(capacityClient),
      respondToCapacityLog: genAuthorizedGRPCFn(
        authToken,
        capacityClient.respondToCapacityLog.bind(capacityClient)
      ),
    },
  };
}

type RetryExt = RetryOptions;

function genAuthorizedGRPCFn<REQ extends object, R>(
  authToken: string,
  fn: (req: REQ, ctx?: Partial<CallContext> & RetryExt) => R,
  opts?: { retry: boolean }
) {
  let retry = true;
  if (opts?.retry !== undefined) {
    retry = opts.retry;
  }
  return (req: REQ, ctx?: Partial<CallContext> & RetryExt): R => {
    const metadata = ctx?.metadata || new Metadata();
    metadata.set("authorization", "Bearer " + authToken);
    const params = ctx || ({} as Partial<CallContext> & RetryExt);
    Object.assign(params, { metadata });
    Object.assign(
      params,
      retry
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
        : {}
    );
    return fn(req, params);
  };
}
