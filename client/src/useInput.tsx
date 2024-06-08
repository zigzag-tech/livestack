import { z } from "zod";
import { JobInfo } from "./useJobBinding";
import { useMemo } from "react";

/**
 * Custom hook to create an input feed function for a job.
 *
 * @param {Object} params - The parameters for the hook.
 * @param {JobInfo<any>} params.job - The job information.
 * @param {string} [params.tag] - Optional tag for the input feed.
 * @param {z.ZodType<T>} [params.def] - Optional Zod schema for validation.
 * @returns {Object} An object containing the feed function.
 */
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
