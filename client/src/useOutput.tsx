import { useEffect, useState } from "react";
import { z } from "zod";
import { JobInfo } from "./useJobBinding";
import { StreamQuery } from "@livestack/shared";

export function useOutput<O>({
  job: { specName, uniqueSpecLabel, jobId, connRef },
  tag,
  def,
  query = { type: "lastN", n: 1 },
}: {
  job: JobInfo<any>;
  tag?: string;
  def?: z.ZodType<O>;
  query?: StreamQuery;
}) {
  return useStream<O>({
    job: { specName, uniqueSpecLabel, jobId, connRef },
    tag,
    def,
    type: "output",
    query,
  });
}

export function useStream<O>({
  job: { specName, uniqueSpecLabel, jobId, connRef },
  tag,
  type,
  query,
}: {
  job: JobInfo<any>;
  type: "input" | "output";
  tag?: string;
  def?: z.ZodType<O>;
  query: StreamQuery;
}) {
  const [output, setOutput] = useState<
    {
      data: O;
      timestamp: number;
      chunkId: string;
      tag?: string;
    }[] & {
      last: {
        data: O;
        timestamp: number;
        chunkId: string;
        tag?: string;
      } | null;
    }
  >(Object.assign([], { last: null }));

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
            query,
          },
          (data) => {
            if (query.type === "lastN") {
              setOutput((prev) => {
                if (prev.length >= query.n) {
                  return Object.assign([...prev.slice(1), data], {
                    last: data,
                  });
                }
                return Object.assign([...prev, data], { last: data });
              });
            } else if (query.type === "all") {
              setOutput((prev) =>
                Object.assign([...prev, data], { last: data })
              );
            } else {
              throw new Error(`Unsupported query type "${query.type}".`);
            }
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
