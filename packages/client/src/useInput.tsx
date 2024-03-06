import { z } from "zod";
import { JobInfo } from "./useJobBinding";
import { useCallback } from "react";

export function useInput<T>({
  job: { specName, jobId, connRef },
  tag,
  def,
}: {
  job: JobInfo;
  tag?: string;
  def?: z.ZodType<T>;
}) {
  const feed = useCallback(
    async (data: T) => {
      if (specName && jobId) {
        const conn = connRef.current;
        if (!conn) {
          throw new Error(`Connection not found with jobId "${jobId}".`);
        }

        return await (await conn).feed(data, tag);
      }
    },
    [specName, jobId, tag, connRef]
  );

  return { feed };
}
