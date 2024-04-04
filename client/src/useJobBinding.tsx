import { useEffect, useRef, useState } from "react";
import {
  ClientConnParams,
  JobSocketIOConnection,
  bindNewJobToSocketIO,
} from "./JobSocketIOClient";
import { z } from "zod";

export type JobInfo<P> = {
  specName?: string;
  uniqueSpecLabel?: string;
  jobId?: string | null;
  jobOptions?: P;
  jobOptionsDef?: z.ZodType<P>;
  connRef: React.MutableRefObject<Promise<JobSocketIOConnection> | undefined>;
};

type DeferredClosedConn =
  | {
      status: "connected";
      jobConn: Promise<JobSocketIOConnection>;
      initiateDeferredClose: () => void;
    }
  | {
      status: "closing";
      cancelClose: () => Promise<JobSocketIOConnection>;
    };

const DEFERRED_CLOSED_CONN_CACHE: Record<
  `${string}${"" | `[${string}]`}`,
  DeferredClosedConn | undefined
> = {};

export function useJobBinding<P>({
  socketIOURI,
  socketIOPath,
  socketIOClient,
  specName,
  uniqueSpecLabel,
  authToken,
  jobOptions,
  jobId,
}: ClientConnParams<P> & {
  specName: string;
  uniqueSpecLabel?: string;
  authToken?: string;
}): JobInfo<P> {
  const [status, setStatus] = useState<JobStatus>({
    status: "connecting",
    specName,
    uniqueSpecLabel,
  });
  const clientRef = useRef<Promise<JobSocketIOConnection>>();
  const deferredCloseRef = useRef<() => void>();

  useEffect(() => {
    if (!clientRef.current) {
      try {
        setStatus({
          status: "connecting",
          specName,
          uniqueSpecLabel,
        });

        const existing =
          DEFERRED_CLOSED_CONN_CACHE[
            `${specName}${uniqueSpecLabel ? `[${uniqueSpecLabel}]` : ""}`
          ];

        let connection: Promise<JobSocketIOConnection>;
        if (existing) {
          if (existing.status === "connected") {
            connection = existing.jobConn;
          } else {
            connection = existing.cancelClose();
          }
        } else {
          connection = bindNewJobToSocketIO({
            socketIOURI,
            socketIOPath,
            socketIOClient,
            specName,
            uniqueSpecLabel,
            jobOptions,
            jobId,
          });
          const { initiateDeferredClose } = resetToConnected({
            specName,
            uniqueSpecLabel,
            connection,
          });
          deferredCloseRef.current = initiateDeferredClose;
        }

        connection.then((c) => {
          setStatus({
            status: "connected",
            jobId: c.jobId,
            specName,
            uniqueSpecLabel,
          });
        });
        clientRef.current = connection;
      } catch (error) {
        setStatus({ status: "error", errorMessage: JSON.stringify(error) });
        console.error("Failed to setup JobSocketIOConnection:", error);
      }
    }

    return () => {
      deferredCloseRef.current?.();
      deferredCloseRef.current = undefined;
      clientRef.current = undefined;
    };
  }, [socketIOURI, socketIOPath, specName, uniqueSpecLabel]);
  return { ...status, connRef: clientRef };
}

function resetToConnected({
  specName,
  uniqueSpecLabel,
  connection,
}: {
  specName: string;
  uniqueSpecLabel?: string;
  connection: Promise<JobSocketIOConnection>;
}) {
  const r = {
    status: "connected" as const,
    jobConn: connection,
    initiateDeferredClose: () => {
      // console.debug("Closing", specName, uniqueSpecLabel);
      const timeout = setTimeout(async () => {
        (await connection).close();
        DEFERRED_CLOSED_CONN_CACHE[
          `${specName}${uniqueSpecLabel ? `[${uniqueSpecLabel}]` : ""}`
        ] = undefined;
      }, 1000 * 5);

      DEFERRED_CLOSED_CONN_CACHE[
        `${specName}${uniqueSpecLabel ? `[${uniqueSpecLabel}]` : ""}`
      ] = {
        status: "closing",
        cancelClose: async () => {
          // console.debug("Cancelling close", specName, uniqueSpecLabel);
          clearTimeout(timeout);
          resetToConnected({ specName, uniqueSpecLabel, connection });
          return connection;
        },
      };
    },
  };
  DEFERRED_CLOSED_CONN_CACHE[
    `${specName}${uniqueSpecLabel ? `[${uniqueSpecLabel}]` : ""}`
  ] = r;
  return r;
}

type JobStatus =
  | {
      status: "connecting";
      specName: string;
      uniqueSpecLabel?: string;
    }
  | {
      status: "connected";
      jobId: string;
      specName: string;
      uniqueSpecLabel?: string;
    }
  | {
      status: "error";
      errorMessage: string;
    };
