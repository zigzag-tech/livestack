import { speechChunkToTextSpec } from "./speech-chunk-to-transcription";
import OpenAI, { toFile } from "openai";
import { v4 } from "uuid";
import axios from "axios";
import { z } from "zod";

export const SPEECH_REC_JOB_PREFIX = "speech-rec-one";
export const localWhisperConfigSchema = z.optional(
  z.object({
    whisperEndpoint: z.string(),
    model: z.enum(["tiny", "base", "small", "medium", "large", "large-v3"]),
  })
);
const model = process.env.WHISPER_MODEL || "large-v3";
if (
  model !== "tiny" &&
  model !== "base" &&
  model !== "small" &&
  model !== "medium" &&
  model !== "large" &&
  model !== "large-v3"
) {
  throw new Error(`Invalid model specified: ${model}`);
}
export const speechChunkToTranscriptionLocalWhisperWorkerDef =
  speechChunkToTextSpec.defineWorker({
    instanceParamsDef: localWhisperConfigSchema,
    processor: async ({ output, input, workerInstanceParams }) => {
      let prev_input = "";
      for await (const data of input) {
        const { wavb64Str } = data;
        console.log(
          "Local whisper worker received input length: ",
          wavb64Str.length
        );
        const audioData = Buffer.from(wavb64Str, "base64");
        const transcribedText = await transcribeAudioData({
          audioData,
          config: {
            useCloudWhisper: false,
            whisperEndpoint:
              process.env.WHISPER_ENDPOINT || "http://127.0.0.1:5500",
            model,
            ...workerInstanceParams,
          },
        });
        console.debug("Transcribed text: ", transcribedText);

        await output.emit({ transcript: transcribedText });
      }

      prev_input = "";

      // return remaining prev_input if it's not empty string
      if (prev_input) {
        return { transcript: prev_input };
      }
    },
  });

export const speechChunkToTranscriptionOpenAIWorkerDef =
  speechChunkToTextSpec.defineWorker({
    processor: async ({ output, input }) => {
      let prev_input = "";
      const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });
      for await (const data of input) {
        const { wavb64Str } = data;
        console.log(
          "OpenAI worker received input length: ",
          wavb64Str.length
        );
        const audioData = Buffer.from(wavb64Str, "base64");
        const transcribedText = await transcribeAudioData({
          audioData,
          config: {
            useCloudWhisper: true,
            openai,
          },
        });
        console.debug("Transcribed text: ", transcribedText);

        await output.emit({ transcript: transcribedText });
      }

      prev_input = "";

      // return remaining prev_input if it's not empty string
      if (prev_input) {
        return { transcript: prev_input };
      }
    },
  });

async function transcribeAudioData({
  audioData,
  config,
}: {
  audioData: Buffer;
  config:
    | {
        useCloudWhisper: true;
        openai: OpenAI;
      }
    | ({
        useCloudWhisper: false;
      } & z.infer<typeof localWhisperConfigSchema>);
}) {
  if (config.useCloudWhisper) {
    const res = await config.openai.audio.transcriptions.create({
      file: await toFile(audioData, `${v4()}.mp3`, {
        type: "mp3",
      }),
      model: "whisper-1",
      temperature: 0,
    });
    return res.text;
  } else {
    const r = await axios.post(
      `${config.whisperEndpoint}/transcribe?model=${config.model}`,
      audioData,
      {
        headers: {
          "Content-Type": "audio/wav",
        },
      }
    );
    return r.data.transcription as string;
  }
}
