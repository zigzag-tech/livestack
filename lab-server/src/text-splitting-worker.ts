import { splittingStatsSchema } from "@livestack/lab-internal-common";
import { z } from "zod";
import { genSplitterIterable } from "./textSplitter";
import { JobSpec } from "@livestack/core";
const DEFAULT_CHUNK_SIZE = 500;
const AUTO_START_WORKER = true;

export const textSplittingSpec = JobSpec.define({
  name: "text-splitting",
  input: z.object({
    documentId: z.string(),
    content: z.string(),
  }),
  output: {
    default: z.object({
      chunk: z.string(),
      documentId: z.string(),
    }),
    "splitting-stats": splittingStatsSchema,
  },
  jobOptions: z.object({
    chunkSize: z.number().optional().default(DEFAULT_CHUNK_SIZE),
  }),
});

export const textSplittingWorkerDef = textSplittingSpec.defineWorker({
  autostartWorker: AUTO_START_WORKER,
  processor: async ({ input, output, jobOptions }) => {
    let currentDocumentId : string | null = null;
    let splitter : ReturnType<typeof genSplitterIterable> | null = null;

    for await (const { content: chunk, documentId } of input) {
      if(currentDocumentId !== documentId) {
        // flush the current splitter
        splitter?.flushRemaining();

        // create new splitter for the new document
        splitter = genSplitterIterable({
          chunkSize: jobOptions.chunkSize || DEFAULT_CHUNK_SIZE,
        });
        currentDocumentId = documentId;

        // start new emit loop 
        (async () => {
          for await (const chunk of splitter) {
            await output.emit({
              documentId,
              chunk, 
            });
          }
        })();
      }
      
      const { numCharsInCache } = await splitter!.feed(chunk);
      await output("splitting-stats").emit({
        numCharsInCache,
        total: DEFAULT_CHUNK_SIZE,
      });
    }

    
  },
});
