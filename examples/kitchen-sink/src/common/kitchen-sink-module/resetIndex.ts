import { JobSpec, LiveWorker } from "@livestack/core";
import { z } from "zod";
import { resetIndex } from "./indexMap";

export function getResetIndexWorkerDefs() {
  const resetIndexJobSpec = JobSpec.define({
    name: "reset-index-job",
    input: z.string(),
    output: z.string(),
  });

  const resetIndexWorkerDef = LiveWorker.define({
    jobSpec: resetIndexJobSpec,
    processor: async ({ input, output, logger }) => {
      const indexNameDirMapping: Record<string, string> = {
        "text-input": "rag_index",
        "audio-input": "audio_index",
      };
      for await (const data of input) {
        if (data in indexNameDirMapping) {
          try {
            await resetIndex({ indexName: indexNameDirMapping[data] });
            await output.emit(
              `Successfully reset the index: ${indexNameDirMapping[data]}`
            );
          } catch (error) {
            console.error("Error resetting the index:", error);
          }
        }
      }
    },
  });

  return { resetIndexJobSpec, resetIndexWorkerDef };
}
