import {
  rawPCMToWavSpec,
  speechChunkToTextSpec,
} from "@livestack/transcribe/server";
import { Liveflow, conn, expose } from "@livestack/core";
import { SPEECH_LIVEFLOW_NAME } from "../common/defs";
import { translationSpec } from "@livestack/translate-server";
import { titleSummarizerSepc } from "@livestack/summarizer/server";
import { textSplittingSpec } from "@livestack/lab-internal-server";

// Define the speech liveflow
export const speechLiveflow = Liveflow.define({
  name: SPEECH_LIVEFLOW_NAME,
  connections: [
    // Connection from raw PCM to WAV
    conn({
      from: rawPCMToWavSpec,
      transform: ({ wavb64Str }) => ({ wavb64Str, whisperType: "openai" }),
      to: speechChunkToTextSpec,
    }),
    // Connection from speech chunk to text
    conn({
      from: speechChunkToTextSpec,
      transform: ({ transcript }) => transcript,
      to: textSplittingSpec,
    }),
    // Connection from text splitting to title summarizer
    conn({
      from: textSplittingSpec,
      transform: (chunkText) => ({ transcript: chunkText, llmType: "openai" }),
      to: titleSummarizerSepc,
    }),
    // Connection from speech chunk to translation (will not be a perfect translation)
    conn({
      from: speechChunkToTextSpec,
      transform: ({ transcript }) => ({
        toLang: "French",
        text: transcript,
        llmType: "openai",
      }),
      to: translationSpec,
    }),
  ],
  exposures: [
    // Expose the default input for raw PCM to WAV
    expose(rawPCMToWavSpec.input.default, "input-default"),
    // Expose the default output for speech chunk to text
    expose(speechChunkToTextSpec.output.default, "transcription"),
    // Expose the default output for title summarizer
    expose(titleSummarizerSepc.output.default, "summarized-title"),
    // Expose the default output for translation
    expose(translationSpec.output.default, "translation"),
  ],
});
