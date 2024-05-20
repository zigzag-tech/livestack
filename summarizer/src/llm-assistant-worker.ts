import { z } from "zod";
import { JobSpec } from "@livestack/core";
import { summarizedTitleDef } from "./defs";
import { transcriptInputDef } from "./llmUtils";
import { getQueuedJobOrCreate } from "./llmChildJobManager";

// const jobOptionsDef = z.object({
//   maxTokens: z.number().default(100).optional(),
//   temperature: z.number().default(0.7).optional(),
//   topP: z.number().default(1).optional(),
//   presencePenalty: z.number().default(0).optional(),
//   frequencyPenalty: z.number().default(0).optional(),
//   bestOf: z.number().default(1).optional(),
//   n: z.number().default(1).optional(),
//   stream: z.boolean().default(false).optional(),
//   stop: z.array(z.string()).default(["\n"]).optional(),
//   llmType: z.enum(["openai", "lepton", "ollama"]).default("ollama").optional(),
// });

export const titleSummarizerSepc = new JobSpec({
  name: "live-title-assistant",
  // jobOptions: jobOptionsDef,
  input: transcriptInputDef.extend({
    llmType: z
      .enum(["openai", "lepton", "ollama"])
      .default("ollama")
      .optional(),
  }),
  output: summarizedTitleDef,
});

export const titleSummarizerWorker = titleSummarizerSepc.defineWorker({
  processor: async ({ input, output, invoke }) => {
    for await (const data of input) {
      const { transcript, llmType } = data;
      const job = await getQueuedJobOrCreate({
        llmType: llmType || "ollama",
      });
      const { input: childInput, output: childOutput } = job;
      await childInput.feed({ transcript });
      const r = await childOutput.nextValue();
      if (!r) {
        throw new Error("No response from LLM");
      }
      await output.emit(r.data);
    }
  },
});
