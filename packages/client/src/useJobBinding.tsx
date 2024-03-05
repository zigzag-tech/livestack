import { useEffect, useRef, useState } from "react";
import {
  ClientConnParams,
  JobSocketIOConnection,
  bindNewJobToSocketIO,
} from "./JobSocketIOClient";

export function useJobBinding({
  socketIOURI,
  socketIOPath,
  socketIOClient,
  specName,
  uniqueSpecLabel,
  authToken = "",
}: ClientConnParams & {
  specName: string;
  uniqueSpecLabel?: string;
  authToken?: string;
}) {
  const [status, setStatus] = useState<JobStatus>({
    status: "connecting",
  });
  const clientRef = useRef<Promise<JobSocketIOConnection>>();

  useEffect(() => {
    if (!clientRef.current) {
      try {
        setStatus({ status: "connecting" });
        const connection = bindNewJobToSocketIO({
          socketIOURI,
          socketIOPath,
          socketIOClient,
          specName,
          uniqueSpecLabel,
          authToken,
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
      clientRef.current?.then((c) => {
        c.close();
      });
      clientRef.current = undefined;
    };
  }, [socketIOURI, socketIOPath, specName, uniqueSpecLabel]);

  return { ...status, connRef: clientRef };
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
