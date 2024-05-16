import {
  rawPCMToWavSpec,
  speechChunkToTextSpec,
} from "@livestack/transcribe-server";
import { Liveflow, conn, expose } from "@livestack/core";
import { SPEECH_LIVEFLOW_NAME } from "../common/defs";
import { translationSpec } from "@livestack/translate-server";
import { titleSummarizerSepc } from "@livestack/lab-internal-server";
import { textSplittingSpec } from "@livestack/lab-internal-server";

export const speechLiveflow = Liveflow.define({
  name: SPEECH_LIVEFLOW_NAME,
  connections: [
    conn({
      from: rawPCMToWavSpec,
      to: speechChunkToTextSpec,
    }),
    conn({
      from: speechChunkToTextSpec,
      transform: ({ transcript }) => transcript,
      to: textSplittingSpec,
    }),
    conn({
      from: textSplittingSpec,
      transform: (chunkText) => ({ transcript: chunkText }),
      to: titleSummarizerSepc,
    }),
    conn({
      from: textSplittingSpec,
      transform: (chunkText) => ({
        toLang: "French",
        text: chunkText,
      }),
      to: translationSpec,
    }),
  ],
  exposures: [
    expose(rawPCMToWavSpec.input.default, "input-default"),
    expose(speechChunkToTextSpec.output.default, "transcription"),
    expose(titleSummarizerSepc.output.default, "summarized-title"),
    expose(translationSpec.output.default, "translation"),
  ],
});
