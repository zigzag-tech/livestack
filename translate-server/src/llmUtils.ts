import { JobSpec } from "@livestack/core";
import { z } from "zod";
import { generateTranslationJson } from "./llmCatalog";
import {
  translationInputSchema,
  translationOutputSchema,
} from "@livestack/lab-internal-common";

const translationExamples = [
  {
    role: "system" as const,
    content:
      'You are a helpful assistant. Your job is to translate content that the user provided from one language to another. Respond in JSON format e.g. { "translated": "..." }',
  },
  {
    role: "user" as const,
    content: "Translate to Chinese: I'm enjoying a hamburger right now.",
  },
  {
    role: "assistant" as const,
    content: '{"translated": "我现在正在享用一个汉堡。"}',
  },
  {
    role: "user" as const,
    content: "Translate to French: She reads a book.",
  },
  {
    role: "assistant" as const,
    content: '{"translated": "Elle lit un livre."}',
  },
  {
    role: "user" as const,
    content: "Translate to English: 他正在弹吉他。",
  },
  {
    role: "assistant" as const,
    content: '{"translated": "He is playing the guitar."}',
  },
];

async function translate(
  input: z.infer<typeof translationInputSchema> &
    (
      | {
        llmType: "ollama";
      }
      | { llmType: "openai" }
    )
): Promise<z.infer<typeof translationOutputSchema>> {
  const { llmType, toLang, text } = input;
  const messages = [
    ...translationExamples,
    {
      role: "user" as const,
      content: `Translate to ${toLang}: ${text}`,
    },
  ];
  if (llmType === "ollama") {
    return await generateTranslationJson<{ translated: string }>({
      purpose: "translation-local",
      messages,
      schema: z.object({ translated: z.string() }),
    });
  } else {
    return await generateTranslationJson<{ translated: string }>({
      purpose: "translation-openai",
      messages,
      schema: z.object({ translated: z.string() }),
      parameters: {
        temperature: 1,
        max_tokens: 256,
        top_p: 1,
        frequency_penalty: 0,
        presence_penalty: 0,
      },
    });
  }
}

const localLLMTranslationSpec = new JobSpec({
  name: "local-llm-translation",
  input: translationInputSchema,
  output: translationOutputSchema,
});

const openAILLMTranslationSpec = new JobSpec({
  name: "openai-llm-translation",
  input: translationInputSchema,
  output: translationOutputSchema,
});

export const llmSelectorSpec = new JobSpec({
  name: "llm-selector-translation",
  jobOptions: z.object({
    llmType: z.enum(["openai", "ollama"]).default("ollama"),
  }),
  input: translationInputSchema,
  output: translationOutputSchema,
});

export const localLLMTranslationWorker = localLLMTranslationSpec.defineWorker({
  processor: async ({ input, output }) => {
    for await (const data of input) {
      const r = await translate({ ...data, llmType: "ollama" });
      output.emit(r);
    }
  },
});

export const openAILLMTranslationWorker = openAILLMTranslationSpec.defineWorker(
  {
    processor: async ({ input, output }) => {
      if (!process.env.OPENAI_API_KEY) {
        throw new Error(
          "OPENAI_API_KEY is not defined. Please set it as an environment variable."
        );
      }
      for await (const data of input) {
        const r = await translate({ ...data, llmType: "openai" });
        output.emit(r);
      }
    },
  }
);

export const llmSelectorWorker = llmSelectorSpec.defineWorker({
  processor: async ({ input, output, jobOptions, invoke }) => {
    const { llmType } = jobOptions;
    const { input: childInput, output: childOutput } =
      llmType === "ollama"
        ? await localLLMTranslationSpec.enqueueJob()
        : await openAILLMTranslationSpec.enqueueJob();

    for await (const data of input) {
      await childInput.feed(data);
      const r = await childOutput.nextValue();

      if (!r) {
        throw new Error("No output from child worker");
      }
      await output.emit(r.data);

      // if (llmType === "openai") {
      //   const r = await invoke({
      //     spec: openAILLMTranslationSpec,
      //     inputData: data,
      //   });
      //   output.emit(r);
      // } else {
      //   const r = await invoke({
      //     spec: localLLMTranslationSpec,
      //     inputData: data,
      //   });
      //   output.emit(r);
      // }
    }
  },
});
