import { useEffect, useState } from "react";
import { JobSocketIOConnection } from "./JobSocketIOClient";
import { z } from "zod";

export function useOutput<O>({
  job: { specName, uniqueSpecLabel, jobId, connRef },
  tag,
  def,
}: {
  job: {
    specName?: string;
    uniqueSpecLabel?: string;
    jobId?: string;
    connRef: React.MutableRefObject<Promise<JobSocketIOConnection> | undefined>;
  };
  tag?: string;
  def?: z.ZodType<O>;
}) {
  const [output, setOutput] = useState<{
    data: O;
    timestamp: number;
    messageId: string;
    tag?: string;
  } | null>(null);

  useEffect(() => {
    if (specName && jobId) {
      const conn = connRef.current;
      if (!conn) {
        throw new Error(`Connection not found with jobId "${jobId}".`);
      }

      conn.then((conn) =>
        conn.subToStream<O>(
          {
            tag,
          },
          (data) => {
            setOutput({ ...data, tag });
          }
        )
      );
    }
  }, [specName, uniqueSpecLabel, jobId, tag]);

  return output;
}
