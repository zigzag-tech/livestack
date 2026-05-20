import { JobSpec } from "@livestack/core";
import { z } from "zod";
import { summarizedTitleDef } from "../common/defs";
import {
  LLAMA2_13B_MODEL,
  LLAMA2_70B_MODEL,
  executeOpenAILikeLLMAPIChat,
} from "./leptonUtils";
import { generateSimpleResponseOllama } from "./ollamaUtils";
import { fewShotExamples } from "./ollamaUtils";
import { generateLivestackJson } from "./llmCatalog";
const AUTO_START_WORKER = true;

export const transcriptInputDef = z.object({
  transcript: z.string(),
});

const localLLMSummarizedTitleSpec = new JobSpec({
  name: "local-llm-summarized-title",
  input: transcriptInputDef,
  output: summarizedTitleDef,
});

const openAILLMSummarizedTitleSpec = new JobSpec({
  name: "openai-llm-summarized-title",
  input: transcriptInputDef,
  output: summarizedTitleDef,
});

const leptonLLMSummarizedTitleSpec = new JobSpec({
  name: "lepton-llm-summarized-title",
  input: transcriptInputDef,
  output: summarizedTitleDef,
});

export const llmSelectorSpec = new JobSpec({
  name: "llm-selector-summarized-title",
  jobOptions: z.object({
    llmType: z.enum(["openai", "ollama", "lepton"]).default("ollama"),
  }),
  input: transcriptInputDef,
  output: summarizedTitleDef,
});

const leptonLLMSummarizedTitleWorker =
  leptonLLMSummarizedTitleSpec.defineWorker({
    processor: async ({ input, output }) => {
      for await (const data of input) {
        let title = "";
        const titleRaw = await executeOpenAILikeLLMAPIChat({
          prompt: data.transcript,
          modelIds: [LLAMA2_70B_MODEL, LLAMA2_13B_MODEL],
        });
        try {
          title = JSON.parse(titleRaw).title;
        } catch (e) {
          console.error("Failed to parse Lepton response", e);
          console.error("Lepton response", titleRaw);
        }
        output.emit({ summarizedTitle: title });
      }
    },
  });

export const localLLMSummarizedTitleWorker =
  localLLMSummarizedTitleSpec.defineWorker({
    autostartWorker: AUTO_START_WORKER,
    processor: async ({ input, output }) => {
      for await (const data of input) {
        let title = "";
        const titleRaw = await generateSimpleResponseOllama({
          format: "json",
          messages: [
            ...fewShotExamples,
            {
              role: "user",
              content: `ORIGINAL TEXT:
\`\`\`
${data.transcript}
\`\`\`

JSON TITLE:
`,
            },
          ],
        });
        try {
          title = JSON.parse(titleRaw).title;
        } catch (e) {
          console.error("Failed to parse Ollama response", e);
          console.error("Ollama response", titleRaw);
        }
        output.emit({ summarizedTitle: title });
      }
    },
  });

const openAILLMSummarizedTitleWorker =
  openAILLMSummarizedTitleSpec.defineWorker({
    processor: async ({ input, output }) => {
      for await (const data of input) {
        let title = "";
        const parsedR = await generateLivestackJson<{ title: string }>({
          purpose: "title-openai",
          messages: [
            ...fewShotExamples,
            {
              role: "user",
              content: `ORIGINAL TEXT:
\`\`\`
${data.transcript}
\`\`\`

JSON TITLE:
`,
            },
          ],
          schema: z.object({ title: z.string() }),
        });
        try {
          title = parsedR.title;
        } catch (e) {
          console.error("Failed to parse OpenAI response", e);
        }

        output.emit({ summarizedTitle: title });
      }
    },
  });

export const llmSelectorSummarizerWorkerDef = llmSelectorSpec.defineWorker({
  autostartWorker: AUTO_START_WORKER,
  processor: async ({ input, output, jobOptions, invoke }) => {
    const { llmType } = jobOptions;
    const job =
      llmType === "openai"
        ? await openAILLMSummarizedTitleWorker.enqueueJob()
        : llmType === "lepton"
        ? await leptonLLMSummarizedTitleWorker.enqueueJob()
        : await localLLMSummarizedTitleWorker.enqueueJob();
    const { input: childInput, output: childOutput } = job;

    for await (const data of input) {
      await childInput.feed(data);
      const r = await childOutput.nextValue();

      if (!r) {
        throw new Error("No output from child worker");
      }

      await output.emit(r.data);
      // if (llmType === "openai") {
      //   const r = await invoke({
      //     spec: openAILLMSummarizedTitleSpec,
      //     inputData: data,
      //   });
      //   output.emit(r);
      // } else if (llmType === "lepton") {
      //   const r = await invoke({
      //     spec: leptonLLMSummarizedTitleSpec,
      //     inputData: data,
      //   });
      //   output.emit(r);
      // } else {
      //   const r = await invoke({
      //     spec: localLLMSummarizedTitleSpec,
      //     inputData: data,
      //   });
      //   output.emit(r);
      // }
    }
  },
});
