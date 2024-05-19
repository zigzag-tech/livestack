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

export const llmSelectorSpec = new JobSpec({
  name: "llm-selector-transcription",
  jobOptions: z.object({
    llmType: z.enum(["openai", "ollama"]).default("ollama"),
  }),
  input: speechChunkToTextInput,
  output: speechChunkToTextOutput,
});

const localLLMTranscriptionSpec = new JobSpec({
  name: "local-llm-transcription",
  jobOptions: localWhisperConfigSchema,
  input: speechChunkToTextInput,
  output: speechChunkToTextOutput,
});

const openAILLMTranscriptionSpec = new JobSpec({
  name: "openai-llm-transcription",
  input: speechChunkToTextInput,
  output: speechChunkToTextOutput,
});

const localLLMTranscriptionWorker = localLLMTranscriptionSpec.defineWorker({
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

const openAILLMTranscriptionWorker = openAILLMTranscriptionSpec.defineWorker({
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

const llmSelectorWorker = llmSelectorSpec.defineWorker({
  processor: async ({ input, output, jobOptions, invoke }) => {
    const { llmType } = jobOptions;
    const job =
      llmType === "openai"
        ? await openAILLMTranscriptionWorker.enqueueJob()
        : await localLLMTranscriptionWorker.enqueueJob({
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
      output.emit(r.data);

      // if (llmType === "openai") {
      // const r = await invoke({
      //   spec: openAILLMTranscriptionSpec,
      //   inputData: data,
      // });
      // output.emit(r);
      // } else {
      //   const r = await invoke({
      //     spec: localLLMTranscriptionSpec,
      //     inputData: data,
      //     jobOptions: {
      //       whisperEndpoint:
      //         process.env.WHISPER_ENDPOINT || "http://localhost:5500",
      //       model: "large-v3" as const,
      //     },
      //   });
      //   output.emit(r);
      // }
    }
  },
});
