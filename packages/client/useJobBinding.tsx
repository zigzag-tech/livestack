import { useRef, useState } from "react";
import useDeepCompareEffect from "use-deep-compare-effect";
import {
  ClientConnParams,
  JobSocketIOConnection,
  bindNewJobToSocketIO,
} from "./JobSocketIOClient";

export function useJobBinding<Ks extends string>({
  socketIOURI,
  socketIOPath,
  socketIOClient,
  specName,
  uniqueSpecLabel,
  streamsToWatch = [{ key: "default" as Ks, mode: "replace" }],
}: ClientConnParams & {
  specName: string;
  uniqueSpecLabel?: string;
  streamsToWatch?: {
    key: Ks;
    mode: "append" | "replace";
    specName?: string;
    uniqueSpecLabel?: string;
    type?: "output" | "input";
  }[];
}) {
  const clientRef = useRef<JobSocketIOConnection>();

  const [outputsByTag, setOutputsByTag] = useState<{
    [key in Ks]:
      | { timestamp: number; data: any }
      | { timestamp: number; data: any }[]
      | null;
  }>(
    Object.fromEntries(
      streamsToWatch.map(({ key = "default" }) => [key, null])
    ) as {
      [key in Ks]: null;
    }
  );

  useDeepCompareEffect(() => {
    let isCancelled = false;

    async function setupConnection() {
      try {
        const connection = await bindNewJobToSocketIO<Ks>({
          socketIOURI,
          socketIOPath,
          socketIOClient,
          specName,
          uniqueSpecLabel,
        });

        if (!isCancelled) {
          clientRef.current = connection;

          streamsToWatch.forEach(
            ({ key = "default" as Ks, mode, specName, uniqueSpecLabel }) => {
              connection.subToOutput(
                {
                  key,
                  specName,
                  uniqueSpecLabel,
                },
                (data) => {
                  setOutputsByTag((prevOutputs) => {
                    if (mode === "replace") {
                      return { ...prevOutputs, [key]: data };
                    } else {
                      // mode === "append"
                      return {
                        ...prevOutputs,
                        [key]: [
                          ...((prevOutputs[key] as {
                            timestamp: number;
                            data: any;
                          }[]) || []),
                          data,
                        ],
                      };
                    }
                  });
                }
              );
            }
          );
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

  return { feed, outputsByTag };
}
