import OpenAI from "openai";
import {
  LLAMA2_13B_MODEL,
  LLAMA2_70B_MODEL,
  baseInstruction,
  executeOpenAILikeLLMAPIChat,
} from "./leptonUtils";
import { z } from "zod";
import { JobSpec } from "@livestack/core";
import { summarizedTitleDef } from "./defs";
import { generateResponseOllama } from "./ollamaUtils";

const jobOptionsDef = z.object({
  maxTokens: z.number().default(100).optional(),
  temperature: z.number().default(0.7).optional(),
  topP: z.number().default(1).optional(),
  presencePenalty: z.number().default(0).optional(),
  frequencyPenalty: z.number().default(0).optional(),
  bestOf: z.number().default(1).optional(),
  n: z.number().default(1).optional(),
  stream: z.boolean().default(false).optional(),
  stop: z.array(z.string()).default(["\n"]).optional(),
  llmType: z.enum(["openai", "lepton", "ollama"]).default("ollama").optional(),
});
const inputDef = z.object({
  transcript: z.string(),
});

export const titleSummarizerSepc = new JobSpec({
  name: "gpt-assistant",
  jobOptions: jobOptionsDef,
  input: { default: inputDef },
  output: { default: summarizedTitleDef },
});

export const ollamaTitleSummarizerWorkerDef = titleSummarizerSepc.defineWorker({
  processor: async ({ input, output, jobOptions }) => {
    let gptSetups:
      | {
          llm: "openai";
          openai: OpenAI;
          assistant: OpenAI.Beta.Assistants.Assistant;
          thread: OpenAI.Beta.Threads.Thread;
        }
      | { llm: "lepton" }
      | { llm: "ollama" };

    const { llmType } = jobOptions;

    if (llmType === "openai") {
      try {
        const openai = new OpenAI();
        const assistant = await getOrCreateGPTAssistant({
          name: "Lower Third Generator",
          instructions: baseInstruction,
          model: "gpt-3.5-turbo",
          openai,
        });

        const thread = await openai.beta.threads.create();
        gptSetups = { llm: "openai", openai, assistant, thread };
      } catch (error) {
        console.error(`Unable to use GPT Assistant: ${error}`);
        gptSetups = { llm: "lepton" };
      }
    } else if (llmType === "lepton") {
      gptSetups = { llm: "lepton" };
    } else {
      gptSetups = { llm: "ollama" };
    }

    for await (const data of input) {
      const { transcript: text } = data;
      // console.info("GPT assistant worker input transcript: ", text);
      let title: null | string = null;

      if (gptSetups.llm === "openai") {
        const { assistant, thread, openai } = gptSetups;
        await makeSureExistingRunsCompleted(openai, thread.id);
        const newMessage = await openai.beta.threads.messages.create(
          thread.id,
          {
            role: "user",
            content: text,
          }
        );
        const newRun = await openai.beta.threads.runs.create(thread.id, {
          assistant_id: assistant.id,
        });

        let runStatus = "";

        while (runStatus !== "completed") {
          const retrievedRun = await openai.beta.threads.runs.retrieve(
            thread.id,
            newRun.id
          );
          runStatus = retrievedRun.status;
          await sleep(300);
        }

        const messages = await openai.beta.threads.messages.list(thread.id);
        title = (messages.data[0].content[0] as any).text.value || "";
        title = extractBetweenQuotes(title as string);
        // await sleep(1000);
        // title = "Summarized " + text;

        // get only text after "TITLE:"
        const arr = title.split("TITLE:");
        title = arr[arr.length - 1].trim();
      } else if (gptSetups.llm === "lepton") {
        // use Lepton as back up when OpenAI is down
        title = await executeOpenAILikeLLMAPIChat({
          prompt: text,
          modelIds: [LLAMA2_70B_MODEL, LLAMA2_13B_MODEL],
        });
        title = extractBetweenQuotes(title);
        // get only text after "TITLE:"
        const arr = title.split("TITLE:");
        title = arr[arr.length - 1].trim();
      } else {
        const titleRaw = await generateResponseOllama(text);
        try {
          title = JSON.parse(titleRaw).title || "...";
        } catch (e) {
          console.error("Failed to parse Ollama response", e);
          console.error("Ollama response", titleRaw);
          title = "...";
        }
        console.log("gpt assistant summarized title", title);
      }
      await output.emit({
        summarizedTitle: title || "...",
      });
    }
    // await signalEnd();
  },
});

async function getOrCreateGPTAssistant({
  name,
  model,
  instructions,
  openai,
}: {
  name: string;
  model: string;
  instructions: string;
  openai: OpenAI;
}) {
  const assistants = await openai.beta.assistants.list();
  const matchedAssistants = assistants.data.filter(
    (asst) => name === asst.name && model === asst.model
  );
  if (matchedAssistants.length > 0) {
    return matchedAssistants[0];
  } else {
    const newAssistant = await openai.beta.assistants.create({
      name,
      instructions,
      model,
    });
    return newAssistant;
  }
}

async function makeSureExistingRunsCompleted(openai: OpenAI, threadId: string) {
  let areAllRunsCompleted = false;
  while (!areAllRunsCompleted) {
    const runs = await openai.beta.threads.runs.list(threadId);
    areAllRunsCompleted =
      runs.data.filter((r) => r.status !== "completed").length === 0;
    !areAllRunsCompleted && (await sleep(1000));
  }
}

const collectorsByJobId: Record<
  string,
  ReturnType<typeof titleSummarizerSepc.createOutputCollector>
> = {};

export async function getTitleById(jobId: string) {
  if (!collectorsByJobId[jobId]) {
    collectorsByJobId[jobId] = titleSummarizerSepc.createOutputCollector({
      jobId,
    });
  }
  const collector = collectorsByJobId[jobId];
  const jobData = await collector.nextValue();

  return {
    job_id: jobId,
    title: jobData!.data.summarizedTitle || "Livestream",
  };
}

function extractBetweenQuotes(str: string): string {
  // Find the first double quote
  const firstQuoteIndex = str.indexOf('"');

  // If there is no double quote, return the original string
  if (firstQuoteIndex === -1) {
    return str;
  }

  // Find the second double quote
  const secondQuoteIndex = str.indexOf('"', firstQuoteIndex + 1);

  // If there is no second double quote, return the original string
  if (secondQuoteIndex === -1) {
    return str;
  }

  // Extract and return the content between the quotes
  return str.substring(firstQuoteIndex + 1, secondQuoteIndex);
}

export async function sleep(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}
