import { z } from "zod";
import { JobInfo } from "./useJobBinding";

export function useInput<T>({
  job: { specName, jobId, connRef },
  tag,
  def,
}: {
  job: JobInfo;
  tag?: string;
  def?: z.ZodType<T>;
}) {
  const feed = async (data: T) => {
    if (specName && jobId) {
      const conn = connRef.current;
      if (!conn) {
        throw new Error(`Connection not found with jobId "${jobId}".`);
      }

      return await (await conn).feed(data, tag);
    }
  };

  return { feed };
}
