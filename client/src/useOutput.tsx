import { z } from "zod";
import { JobInfo } from "./useJobBinding";
import { StreamQuery } from "@livestack/shared";
import { useStream } from "./useStream";

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
