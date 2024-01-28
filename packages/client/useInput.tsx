import { JobSocketIOConnection } from "./JobSocketIOClient";
import { z } from "zod";

export function useInput<T>({
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
  def?: z.ZodType<T>;
}) {
  const feed = async (data: T) => {
    console.log(specName, jobId, connRef);
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
