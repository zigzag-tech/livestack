import { useEffect, useRef, useState } from "react";
import {
  ClientConnParams,
  JobSocketIOConnection,
  bindNewJobToSocketIO,
} from "./JobSocketIOClient";
import { z } from "zod";

export function useOutput<O>({
  job: {
  specName,
  uniqueSpecLabel,
  jobId,connRef, 
  },
  tag,
  def,
}: {
  job: {
    specName?: string;
    uniqueSpecLabel?: string;
    jobId?: string;
    connRef:  React.MutableRefObject<Promise<JobSocketIOConnection> | undefined>;
  },
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
      const conn = connRef.current;
      if (!conn) {
        throw new Error(`Connection not found with jobId "${jobId}".`);
      }

      conn.then(conn => conn.subToStream<O>(
        {
          tag,
        },
        (data) => {
          setOutput({ ...data, tag });
        }
      ));
    }
  }, [specName, uniqueSpecLabel, jobId, tag]);

  return output;
}

export function useInput<T>({
  job: {
    specName,
    uniqueSpecLabel,
    jobId,connRef
    },
  tag,
  def,
}: {
  job: {
    specName?: string;
    uniqueSpecLabel?: string;
    jobId?: string;
    connRef:  React.MutableRefObject<Promise<JobSocketIOConnection> | undefined>;
  },
  tag: string;
  def?: z.ZodType<T>;
}) {
  const feed = async (data: T) => {
    console.log(specName, jobId, connRef)
    if (specName && jobId) {
      const conn = connRef.current;
      if (!conn) {
        throw new Error(`Connection not found with jobId "${jobId}".`);
      }

      return await (await conn).feed(data, tag);
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
  const clientRef = useRef<Promise<JobSocketIOConnection>>();

  useEffect(() => {
  
    if(!clientRef.current) {
      try {
        setStatus({ status: "connecting" });
        const connection =  bindNewJobToSocketIO({
          socketIOURI,
          socketIOPath,
          socketIOClient,
          specName,
          uniqueSpecLabel,
        });
        clientRef.current = connection;
       connection.then((c) => {
          setStatus({
            status: "connected",
            jobId: c.jobId,
            specName,
            uniqueSpecLabel,
          });
        });
      } catch (error) {
        setStatus({ status: "error", errorMessage: JSON.stringify(error) });
        console.error("Failed to setup JobSocketIOConnection:", error);
      }
    }

    return () => {
     clientRef.current?.then((c) => c.close());
     clientRef.current = undefined;
    };
  }, [socketIOURI, socketIOPath, specName, uniqueSpecLabel]);

  return {...status, connRef: clientRef};
}



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
        !cumulative.reverse().some((c2) => c2.messageId === c!.messageId)
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
