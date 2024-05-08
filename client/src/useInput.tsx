import { z } from "zod";
import { JobInfo } from "./useJobBinding";
import { useCallback, useEffect, useMemo } from "react";

export function useInput<T>({
  job: { specName, jobId, connRef },
  tag,
  def,
}: {
  job: JobInfo<any>;
  tag?: string;
  def?: z.ZodType<T>;
}) {
  const feed = useMemo(() => {
    if (specName && jobId) {
      return async (data: T) => {
        const conn = connRef.current;
        if (!conn) {
          throw new Error(`Connection not found with jobId "${jobId}".`);
        }

        return await (await conn).feed(data, tag);
      };
    } else {
      return null;
    }
  }, [specName, jobId, tag, connRef]);

  return { feed };
}
