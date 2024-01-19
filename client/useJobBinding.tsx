import { useRef, useState } from "react";
import useDeepCompareEffect from "use-deep-compare-effect";
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
  streamsToWatch = [{ key: "default", mode: "replace" }],
}: ClientConnParams & {
  specName: string;
  uniqueSpecLabel?: string;
  streamsToWatch?: {
    key?: string;
    mode: "append" | "replace";
    specName?: string;
    uniqueSpecLabel?: string;
    type?: "output" | "input";
  }[];
}) {
  const clientRef = useRef<JobSocketIOConnection>();

  const [outputsByKey, setOutputsByKey] = useState<{
    [key: string]: any;
  }>({});

  useDeepCompareEffect(() => {
    let isCancelled = false;

    async function setupConnection() {
      try {
        const connection = await bindNewJobToSocketIO({
          socketIOURI,
          socketIOPath,
          socketIOClient,
          specName,
          uniqueSpecLabel,
        });

        if (!isCancelled) {
          clientRef.current = connection;

          streamsToWatch.forEach(({ key = "default", mode }) => {
            connection.subToOutput(key, (data) => {
              setOutputsByKey((prevOutputs) => {
                if (mode === "replace") {
                  return { ...prevOutputs, [key]: data };
                } else {
                  // mode === "append"
                  return {
                    ...prevOutputs,
                    [key]: [...(prevOutputs[key] || []), data],
                  };
                }
              });
            });
          });
        }
      } catch (error) {
        console.error("Failed to setup JobSocketIOConnection:", error);
      }
    }

    setupConnection();

    return () => {
      isCancelled = true;
      if (clientRef.current) {
        // Perform any necessary cleanup here, like unsubscribing from outputs
        clientRef.current.close();
        clientRef.current = undefined;
      }
    };
  }, [socketIOURI, socketIOPath, specName, uniqueSpecLabel, streamsToWatch]);

  const feed = async <T,>(data: T, key: string = "default") => {
    return await clientRef.current?.feed(data, key);
  };

  return { feed, outputsByKey };
}
