import { JobSpec } from "@livestack/core";
import { z } from "zod";
import {
  translationOutputSchema,
  supportedLangs,
} from "@livestack/lab-internal-common";
import { getQueuedJobOrCreate } from "./llmChildJobManager";

export const translationSpec = new JobSpec({
  name: "translation",
  input: {
    default: z.object({
      text: z.string(),
      llmType: z.enum(["openai", "ollama"]).default("ollama").optional(),
    }),
    language: supportedLangs,
  },
  output: translationOutputSchema,
});

export const translationWorker = translationSpec.defineWorker({
  processor: async ({ input, output, invoke }) => {
    let currLang: z.infer<typeof supportedLangs> = supportedLangs.Enum.French;

    const langObs = input("language").observable();
    langObs.subscribe((newLang) => {
      if (newLang) {
        currLang = newLang;
      }
    });

    for await (const data of input("default")) {
      const { llmType, text } = data;
      // obtain translation results from the LLM
      const job = await getQueuedJobOrCreate({ llmType: llmType || "ollama" });
      const { input: childInput, output: childOutput } = job;
      await childInput.feed({ text, toLang: currLang });
      const r = await childOutput.nextValue();

      if (!r) {
        throw new Error("No output from child worker");
      }
      await output.emit(r.data);
    }
  },
});
