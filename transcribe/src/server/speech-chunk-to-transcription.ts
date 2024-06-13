import { JobSpec } from "@livestack/core";
import {
  speechChunkToTextInput,
  speechChunkToTextOutput,
} from "../common/defs";
import { z } from "zod";
import { getQueuedJobOrCreate } from "./whisperChildJobManager";

export const SPEECH_REC_JOB_PREFIX = "speech-rec-one";
const AUTO_START_WORKER = true;

export const speechChunkToTextSpec = new JobSpec({
  name: "speech-chunk-to-transcription",
  input: speechChunkToTextInput.extend({
    whisperType: z.enum(["openai", "local"]).default("local").optional(),
  }),
  output: speechChunkToTextOutput,
});

export const speechChunkToTranscriptionWorkerDef = speechChunkToTextSpec.defineWorker({
  autostartWorker: AUTO_START_WORKER,
  processor: async ({ output, input }) => {
    for await (const data of input) {
      const { wavb64Str, whisperType } = data;
      const job = await getQueuedJobOrCreate({
        whisperType: whisperType || "local",
      });
      const { input: childInput, output: childOutput } = job;
      await childInput.feed({ wavb64Str });
      const r = await childOutput.nextValue();
      if (!r) {
        throw new Error("No response from LLM");
      }
      await output.emit(r.data);
    }
  },
});
