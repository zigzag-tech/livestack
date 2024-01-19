import { Socket } from "socket.io-client";
import { useRef, useState } from "react";
import useDeepCompareEffect from "use-deep-compare-effect";
import { JobSocketIOClient } from "./JobSocketIOClient";

export function useJobBinding({
  socketIOClient,
  socketIOURI,
  socketIOPath,
  specName,
  outputsToWatch = [{ key: "default", mode: "replace" }],
}: {
  socketIOClient?: Socket;
  socketIOURI?: string;
  socketIOPath?: string;
  specName: string;
  outputsToWatch?: {
    key?: string;
    mode: "append" | "replace";
  }[];
}) {
  const jobClientRef = useRef<JobSocketIOClient>();

  const jobInfoRef = useRef<
    | {
        jobId: string;
        inputKeys: string[];
        outputKeys: string[];
      }
    | "not-initialized"
    | "working"
  >("not-initialized");

  const [outputsByKey, setOutputsByKey] = useState<{
    [key: string]: any;
  }>({});

  useDeepCompareEffect(() => {
    if (!jobClientRef.current) {
      jobClientRef.current = new JobSocketIOClient({
        specName,
        socketIOClient,
        socketIOURI,
        socketIOPath,
      });
    }
    const jobClient = jobClientRef.current;

    if (jobInfoRef.current === "not-initialized") {
      jobInfoRef.current = "working";
      jobClient._ensureJobBinding().then(() => {
        const jobInfo = jobClient.jobInfo;
        if (jobInfo) {
          jobInfoRef.current = jobInfo;
          for (const { mode, key = "default" } of outputsToWatch) {
            jobClient.socketIOClient.on(`output:${jobInfo.jobId}/${key}`, (data: any) => {
              // TODO: `data` data structure needs to be corrected. Not always an object.
              const timeStamped = { ...data, _timeStamp: Date.now() };
              if (mode === "replace") {
                setOutputsByKey((prev) => ({ ...prev, [key]: timeStamped }));
              } else if (mode === "append") {
                setOutputsByKey((prev) => ({
                  ...prev,
                  [key]: [...(prev[key] || []), timeStamped],
                }));
              }
            });
          }
        }
      });
        jobInfoRef.current = { jobId, inputKeys, outputKeys };
        for (const { mode, key = "default" } of outputsToWatch) {
          client.on(`output:${jobId}/${key}`, (data: any) => {
            // TODO: `data` data structure needs to be corrected. Not always an object.
            const timeStamped = { ...data, _timeStamp: Date.now() };
            if (mode === "replace") {
              setOutputsByKey((prev) => ({ ...prev, [key]: timeStamped }));
            } else if (mode === "append") {
              setOutputsByKey((prev) => ({
                ...prev,
                [key]: [...(prev[key] || []), timeStamped],
              }));
            }
          });
        }
      });
    }
    return () => {
      if (
        jobInfoRef.current !== "not-initialized" &&
        jobInfoRef.current !== "working"
      ) {
        jobClient.socketIOClient.emit("unbind", { specName, jobId: jobInfoRef.current?.jobId });
      }
    };
  }, [specName, outputsToWatch]);
  }, [specName, outputsToWatch, socketIOClient, socketIOURI, socketIOPath]);

  const feed = async <T,>(data: T, key: string = "default") => {
    if (typeof jobInfoRef.current === "string") {
      throw new Error("Background job not yet running.");
    }
    if (!jobInfoRef.current.inputKeys.includes(key)) {
      throw new Error(`Key ${key} not in inputKeys.`);
    }

    if (!jobClientRef.current) {
      throw new Error("clientRef.current is null");
    }
    jobClientRef.current.socketIOClient.emit(`feed:${jobInfoRef.current.jobId}/${key}`, data);
  };

  return { jobInfo: jobInfoRef.current, feed, outputsByKey };
}
