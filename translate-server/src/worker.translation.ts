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
    const { input: childInput, output: childOutput } =
      await llmSelectorSpec.enqueueJob({ jobOptions: { llmType: "openai" } });

    for await (const data of input) {
      const { llmType, text, toLang } = data;
      await childInput.feed({ text, toLang });
      const r = await childOutput.nextValue();

      if (!r) {
        throw new Error("No output from child worker");
      }
      await output.emit(r.data);

      // const translated = await invoke({
      //   spec: llmSelectorSpec,
      //   inputData: { text, toLang },
      //   jobOptions: { llmType },
      // });
      // await output.emit(translated);
    }
  },
});
