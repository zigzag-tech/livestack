import { useEffect, useRef, useState } from "react";
import useDeepCompareEffect from "use-deep-compare-effect";
import {
  ClientConnParams,
  JobSocketIOConnection,
  bindNewJobToSocketIO,
} from "./JobSocketIOClient";
import { z } from "zod";
import fromPairs from "lodash/fromPairs";
import toPairs from "lodash/toPairs";

export function useJobOutput<O>({
  specName,
  uniqueSpecLabel,
  jobId,
  tag,
  def,
}: {
  specName?: string;
  uniqueSpecLabel?: string;
  jobId?: string;
  tag: string;
  def?: z.ZodType<O>;
}) {
  const [output, setOutput] = useState<{
    data: O;
    timestamp: number;
    messageId: string;
  } | null>(null);

  useDeepCompareEffect(() => {
    if (specName && jobId) {
      const conn = GLOBALCONN_POOL_BY_JOB_ID[jobId];
      if (!conn) {
        throw new Error(`Connection not found with jobId "${jobId}".`);
      }

      return conn.subToOutput<{
        data: O;
        timestamp: number;
        messageId: string;
      }>(
        {
          tag,
        },
        (data) => {
          setOutput(data);
        }
      );
    }
  }, [specName, uniqueSpecLabel, jobId, tag]);

  return output;
}

export function useJobForSession({
  socketIOURI,
  socketIOPath,
  socketIOClient,
  specName,
  uniqueSpecLabel,
}: ClientConnParams & {
  specName: string;
  uniqueSpecLabel?: string;
}) {
  const [status, setStatus] = useState<JobStatus>({
    status: "connecting",
  });

  useDeepCompareEffect(() => {
    const clientRef = useRef<JobSocketIOConnection>();

    async function setupConnection() {
      try {
        setStatus({ status: "connecting" });
        const connection = await bindNewJobToSocketIO({
          socketIOURI,
          socketIOPath,
          socketIOClient,
          specName,
          uniqueSpecLabel,
        });
        clientRef.current = connection;

        GLOBALCONN_POOL_BY_JOB_ID[connection.jobId] = connection;

        setStatus({
          status: "connected",
          jobId: connection.jobId,
          specName,
          uniqueSpecLabel,
        });
        return connection;
      } catch (error) {
        setStatus({ status: "error", errorMessage: JSON.stringify(error) });
        console.error("Failed to setup JobSocketIOConnection:", error);
      }
    }

    const connectP = setupConnection();

    return () => {
      (async () => {
        const conn = await connectP;
        if (conn) {
          // Perform any necessary cleanup here, like unsubscribing from outputs
          delete GLOBALCONN_POOL_BY_JOB_ID[conn.jobId];
          conn.close();
          clientRef.current = undefined;
        }
      })();
    };
  }, [socketIOURI, socketIOPath, specName, uniqueSpecLabel]);

  return status;
}

const GLOBALCONN_POOL_BY_JOB_ID: { [jobId: string]: JobSocketIOConnection } =
  {};

type JobStatus =
  | {
      status: "connecting";
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

export function useCumulative<T>(
  d: {
    data: T;
    timestamp: number;
    messageId: string;
  } | null
) {
  const [cumulative, setCumulative] = useState<
    {
      data: T;
      timestamp: number;
      messageId: string;
    }[]
  >([]);

  useEffect(() => {
    // compare messageId of last cumulative and new data
    // if they are the same, then it's a duplicate, so ignore
    // otherwise, add to cumulative
    if (d) {
      if (
        cumulative.length === 0 ||
        cumulative[cumulative.length - 1].messageId !== d.messageId
      ) {
        setCumulative((c) => [...c, d]);
      }
    }
  }, [d]);

  return cumulative;
}
