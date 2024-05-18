import { JobSpec } from "@livestack/core";
import {
  speechChunkToTextInput,
  speechChunkToTextOutput,
} from "../common/defs";
import { llmSelectorSpec } from "./llmUtils";
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
      for await (const data of input) {
        const { wavb64Str, llmType } = data;
        const r = await invoke({
          spec: llmSelectorSpec,
          inputData: { wavb64Str },
          jobOptions: { llmType },
        });

        await output.emit(r);
      }
    },
  });
