import { useEffect, useRef, useState } from "react";
import {
  ClientConnParams,
  JobSocketIOConnection,
  bindNewJobToSocketIO,
} from "./JobSocketIOClient";
import { z } from "zod";

export function useOutput<O>({
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
    tag: string;
  } | null>(null);

  useEffect(() => {
    if (specName && jobId) {
      const conn = GLOBALCONN_POOL_BY_JOB_ID[jobId];
      if (!conn) {
        throw new Error(`Connection not found with jobId "${jobId}".`);
      }

      return conn.subToStream<{
        data: O;
        timestamp: number;
        messageId: string;
      }>(
        {
          tag,
        },
        (data) => {
          setOutput({ ...data, tag });
        }
      );
    }
  }, [specName, uniqueSpecLabel, jobId, tag]);

  return output;
}

export function useInput<T>({
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
  def?: z.ZodType<T>;
}) {
  const feed = (data: T) => {
    if (specName && jobId) {
      const conn = GLOBALCONN_POOL_BY_JOB_ID[jobId];
      if (!conn) {
        throw new Error(`Connection not found with jobId "${jobId}".`);
      }

      return conn.feed(
        {
          data,
        },
        tag
      );
    }
  };

  return { feed };
}

export function useJobForConnection({
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
  const clientRef = useRef<JobSocketIOConnection>();

  useEffect(() => {
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
          // delete GLOBALCONN_POOL_BY_JOB_ID[conn.jobId];

          // conn.close();
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

type TS<T, Other> = {
  data: T;
  timestamp: number;
  messageId: string;
} & Other;
type TSOrNull<T, Other> = TS<T, Other> | null;

export function useCumulative<
  T1,
  O1,
  T2 = never,
  O2 = never,
  T3 = never,
  O3 = never,
  T4 = never,
  O4 = never,
  T5 = never,
  O5 = never,
  T6 = never,
  O6 = never
>(
  d:
    | TSOrNull<T1, O1>
    | [TSOrNull<T2, O2>]
    | [TSOrNull<T1, O1>, TSOrNull<T2, O2>]
    | [TSOrNull<T1, O1>, TSOrNull<T2, O2>, TSOrNull<T3, O3>]
    | [TSOrNull<T1, O1>, TSOrNull<T2, O2>, TSOrNull<T3, O3>, TSOrNull<T4, O4>]
    | [
        TSOrNull<T1, O1>,
        TSOrNull<T2, O2>,
        TSOrNull<T3, O3>,
        TSOrNull<T4, O4>,
        TSOrNull<T5, O5>
      ]
    | [
        TSOrNull<T1, O1>,
        TSOrNull<T2, O2>,
        TSOrNull<T3, O3>,
        TSOrNull<T4, O4>,
        TSOrNull<T5, O5>,
        TSOrNull<T6, O6>
      ]
) {
  const [cumulative, setCumulative] = useState<
    (
      | TS<T1, O1>
      | TS<T2, O2>
      | TS<T3, O3>
      | TS<T4, O4>
      | TS<T5, O5>
      | TS<T6, O6>
    )[]
  >([]);

  useEffect(() => {
    // compare messageId of last cumulative and new data
    // if they are the same, then it's a duplicate, so ignore
    // otherwise, add to cumulative
    const candidates = (Array.isArray(d) ? d : [d]).filter((d) => !!d);
    for (const c of candidates) {
      // check if cumulative already has this messageId
      // and if not, insert it into cumulative, sorted by timestamp
      if (
        cumulative.length === 0 ||
        cumulative[cumulative.length - 1].messageId !== c!.messageId
      ) {
        const sorted = [...cumulative, c!].sort(
          (a, b) => a.timestamp - b.timestamp
        );
        setCumulative(sorted);
      }
    }
  }, [d]);

  return cumulative;
}
