import { useEffect, useState } from "react";
import { z } from "zod";
import { JobInfo } from "./useJobBinding";

export function useOutput<O>({
  job: { specName, uniqueSpecLabel, jobId, connRef },
  tag,
  def,
}: {
  job: JobInfo<any>;
  tag?: string;
  def?: z.ZodType<O>;
}) {
  return useStream<O>({
    job: { specName, uniqueSpecLabel, jobId, connRef },
    tag,
    def,
    type: "output",
  });
}

export function useStream<O>({
  job: { specName, uniqueSpecLabel, jobId, connRef },
  tag,
  type,
}: {
  job: JobInfo<any>;
  type: "input" | "output";
  tag?: string;
  def?: z.ZodType<O>;
}) {
  const [output, setOutput] = useState<{
    data: O;
    timestamp: number;
    chunkId: string;
    tag?: string;
  } | null>(null);

  useEffect(() => {
    let unsubP: Promise<() => void> | null = null;
    if (specName && jobId) {
      const conn = connRef.current;
      if (!conn) {
        throw new Error(`Connection not found with jobId "${jobId}".`);
      }

      unsubP = conn.then((conn) =>
        conn.subToStream<O>(
          {
            tag,
            type,
          },
          (data) => {
            setOutput({ ...data, tag });
          }
        )
      );
    }

    return () => {
      if (unsubP) {
        unsubP.then((unsub) => unsub());
      }
    };
  }, [specName, uniqueSpecLabel, jobId, tag]);

  return output;
}
