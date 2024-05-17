import { splittingStatsSchema } from "@livestack/lab-internal-common";
import { z } from "zod";
import { genSplitterIterable } from "./textSplitter";
import { JobSpec } from "@livestack/core";
const DEFAULT_CHUNK_SIZE = 500;

export const textSplittingSpec = JobSpec.define({
  name: "text-splitting",
  input: z.string(),
  output: {
    default: z.string(),
    "splitting-stats": splittingStatsSchema,
  },
  jobOptions: z.object({
    chunkSize: z.number().optional().default(DEFAULT_CHUNK_SIZE),
  }),
});

export const textSplittingWorkerDef = textSplittingSpec.defineWorker({
  processor: async ({ input, output, jobOptions }) => {
    const splitter = await genSplitterIterable({
      chunkSize: jobOptions.chunkSize || DEFAULT_CHUNK_SIZE,
    });
    (async () => {
      for await (const chunk of input) {
        const { numCharsInCache } = await splitter.feed(chunk);
        await output("splitting-stats").emit({
          numCharsInCache,
          total: DEFAULT_CHUNK_SIZE,
        });
      }
      // await splitter.flushRemaining();
    })();

    for await (const chunk of splitter) {
      await output.emit(chunk);
    }
  },
});
