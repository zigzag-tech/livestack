import { JobSpec } from "@livestack/core";
import {
  speechChunkToTextInput,
  speechChunkToTextOutput,
} from "../common/defs";
import { transcriptionSelectorSpec } from "./llmUtils";
import { z } from "zod";

export const SPEECH_REC_JOB_PREFIX = "speech-rec-one";

export const speechChunkToTextSpec = new JobSpec({
  name: "speech-chunk-to-transcription",
  input: speechChunkToTextInput.extend({
    llmType: z.enum(["openai", "ollama"]).default("ollama").optional(),
  }),
  output: speechChunkToTextOutput,
});

export const speechChunkToTranscriptionWorkerDef =
  speechChunkToTextSpec.defineWorker({
    processor: async ({ output, input, invoke }) => {
      const { input: childInput, output: childOutput } =
        await transcriptionSelectorSpec.enqueueJob({
          jobOptions: {
            whisperType: "local",
          },
        });
      for await (const data of input) {
        const { wavb64Str, llmType } = data;
        await childInput.feed({ wavb64Str });
        const r = await childOutput.nextValue();
        if (!r) {
          throw new Error("No response from LLM");
        }
        await output.emit(r.data);
      }
    },
  });
