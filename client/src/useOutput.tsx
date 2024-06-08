import { z } from "zod";
import { JobInfo } from "./useJobBinding";
import { StreamQuery } from "@livestack/shared";
import { useStream } from "./useStream";

/**
 * Custom hook to create an output stream for a job.
 *
 * @param {Object} params - The parameters for the hook.
 * @param {JobInfo<any>} params.job - The job information.
 * @param {string} [params.tag] - Optional tag for the output stream.
 * @param {z.ZodType<O>} [params.def] - Optional Zod schema for validation.
 * @param {StreamQuery} [params.query={ type: "lastN", n: 1 }] - Optional query for the stream.
 * @returns {Object} An object containing the stream data.
 */
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
