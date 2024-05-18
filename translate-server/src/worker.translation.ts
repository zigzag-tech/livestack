import { JobSpec } from "@livestack/core";
import { z } from "zod";
import {
  translationInputSchema,
  translationOutputSchema,
} from "@livestack/lab-internal-common";
import { llmSelectorSpec } from "./llmUtils";

export const translationSpec = new JobSpec({
  name: "translation",
  input: translationInputSchema.extend({
    llmType: z.enum(["openai", "ollama"]).default("ollama").optional(),
  }),
  output: translationOutputSchema,
});

export const translationWorker = translationSpec.defineWorker({
  processor: async ({ input, output, invoke }) => {
    for await (const data of input) {
      const { llmType, text, toLang } = data;
      const translated = await invoke({
        spec: llmSelectorSpec,
        inputData: { text, toLang },
        jobOptions: { llmType },
      });
      await output.emit(translated);
    }
  },
});
