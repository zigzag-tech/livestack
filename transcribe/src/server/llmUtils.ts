import OpenAI, { toFile } from "openai";
import { JobSpec } from "@livestack/core";
import { z } from "zod";
import {
  speechChunkToTextInput,
  speechChunkToTextOutput,
} from "../common/defs";
import { v4 } from "uuid";
import axios from "axios";

export const localWhisperConfigSchema = z.object({
  whisperEndpoint: z.string(),
  model: z.enum(["tiny", "base", "small", "medium", "large", "large-v3"]),
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

export const transcriptionSelectorSpec = new JobSpec({
  name: "transcription-selector",
  jobOptions: z.object({
    whisperType: z.enum(["openai", "local"]).default("local"),
  }),
  input: speechChunkToTextInput,
  output: speechChunkToTextOutput,
});

const localTranscriptionSpec = new JobSpec({
  name: "local-transcription",
  jobOptions: localWhisperConfigSchema,
  input: speechChunkToTextInput,
  output: speechChunkToTextOutput,
});

const openAITranscriptionSpec = new JobSpec({
  name: "openai-transcription",
  input: speechChunkToTextInput,
  output: speechChunkToTextOutput,
});

export const localTranscriptionWorker = localTranscriptionSpec.defineWorker({
  processor: async ({ input, output, jobOptions }) => {
    const { whisperEndpoint, model } = jobOptions;

    for await (const { wavb64Str } of input) {
      console.log(
        "Local whisper worker received input length: ",
        wavb64Str.length
      );
      const audioData = Buffer.from(wavb64Str, "base64");
      const transcript = await transcribeAudioData({
        audioData,
        config: {
          useCloudWhisper: false,
          whisperEndpoint,
          model,
        },
      });
      output.emit({ transcript });
    }
  },
});

const openAITranscriptionWorker = openAITranscriptionSpec.defineWorker({
  processor: async ({ input, output }) => {
    const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

    for await (const { wavb64Str } of input) {
      console.log(
        "OpenAI whisper worker received input length: ",
        wavb64Str.length
      );
      const audioData = Buffer.from(wavb64Str, "base64");
      const transcript = await transcribeAudioData({
        audioData,
        config: {
          useCloudWhisper: true,
          openai,
        },
      });
      output.emit({ transcript });
    }
  },
});

export const transcriptionSelectorWorker = transcriptionSelectorSpec.defineWorker({
  autostartWorker: false,
  processor: async ({ input, output, jobOptions, invoke }) => {
    const { whisperType } = jobOptions;
    const job =
      whisperType === "openai"
        ? await openAITranscriptionWorker.enqueueJob()
        : await localTranscriptionWorker.enqueueJob({
            jobOptions: {
              whisperEndpoint:
                process.env.WHISPER_ENDPOINT || "http://localhost:5500",
              model: "large-v3" as const,
            },
          });
    const { input: childInput, output: childOutput } = job;

    for await (const data of input) {
      await childInput.feed(data);
      const r = await childOutput.nextValue();

      if (!r) {
        throw new Error("No output from child worker");
      }
      await output.emit(r.data);
    }
  },
});
