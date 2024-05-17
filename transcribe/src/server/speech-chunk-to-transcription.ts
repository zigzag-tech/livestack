import { JobSpec } from "@livestack/core";
import {
  speechChunkToTextInput,
  speechChunkToTextOutput,
} from "../common/defs";

export const speechChunkToTextSpec = new JobSpec({
  name: "speech-chunk-to-transcription",
  input: speechChunkToTextInput,
  output: speechChunkToTextOutput,
});
