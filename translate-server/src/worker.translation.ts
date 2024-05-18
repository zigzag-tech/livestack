import OpenAI from "openai";
import { JobSpec } from "@livestack/core";
import { z } from "zod";
import { generateSimpleResponseOllama } from "@livestack/summarizer";
import {
  translationInputSchema,
  translationOutputSchema,
} from "@livestack/lab-internal-common";

const translationExamples = [
  {
    role: "system",
    content:
      'You are a helpful assistant. Your job is to translate content that the user provided from one language to another. Respond in JSON format e.g. { "translated": "..." }',
  },
  {
    role: "user",
    content: "Translate to Chinese: I'm enjoying a hamburger right now.",
  },
  {
    role: "assistant",
    content: '{"translated": "我现在正在享用一个汉堡。"}',
  },
  {
    role: "user",
    content: "Translate to French: She reads a book.",
  },
  {
    role: "assistant",
    content: '{"translated": "Elle lit un livre."}',
  },
  {
    role: "user",
    content: "Translate to English: 他正在弹吉他。",
  },
  {
    role: "assistant",
    content: '{"translated": "He is playing the guitar."}',
  },
];

async function translate(
  input: z.infer<typeof translationInputSchema> &
    (
      | {
          llmType: "ollama";
        }
      | { llmType: "openai"; openai: OpenAI }
    )
): Promise<z.infer<typeof translationOutputSchema>> {
  const { llmType, toLang, text } = input;
  const messages = [
    ...translationExamples,
    {
      role: "user",
      content: `Translate to ${toLang}: ${text}`,
    },
  ];
  if (llmType === "ollama") {
    const response = await generateSimpleResponseOllama(messages, "llama3");
    return { translated: response };
  } else {
    const { openai } = input;
    const response: any = await openai.chat.completions.create({
      model: "gpt-3.5-turbo",
      messages: messages as any,
      temperature: 1,
      max_tokens: 256,
      top_p: 1,
      frequency_penalty: 0,
      presence_penalty: 0,
    });

    const translated = JSON.parse(response.choices[0].message.content);
    return translated;
  }
}

export const translationSpec = new JobSpec({
  name: "translation",
  jobOptions: z.object({
    llmType: z.enum(["openai", "ollama"]).default("ollama").optional(),
  }),
  input: translationInputSchema,
  output: translationOutputSchema,
});

export const translationLocalWorker = translationSpec.defineWorker({
  instanceParamsDef: z.object({ useCloudLLM: z.literal(false) }).optional(),
  processor: async ({ input, output }) => {
    for await (const data of input) {
      const translated = await translate({ ...data, llmType: "ollama" });
      await output.emit(translated);
    }
  },
});

export const translationOpenAIWorker = translationSpec.defineWorker({
  instanceParamsDef: z.object({ useCloudLLM: z.literal(true) }),
  processor: async ({ input, output }) => {
    const openai = new OpenAI({
      apiKey: process.env.OPENAI_API_KEY,
    });
    for await (const data of input) {
      const translated = await translate({
        ...data,
        llmType: "openai",
        openai,
      });
      await output.emit(translated);
    }
  },
});
