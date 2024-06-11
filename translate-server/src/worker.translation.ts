import { JobSpec } from "@livestack/core";
import { z } from "zod";
import {
  translationInputSchema,
  translationOutputSchema,
} from "@livestack/lab-internal-common";
import { getQueuedJobOrCreate } from "./llmChildJobManager";
import { supportedLangs } from "../../examples/speech-app/src/common/supportedLangs";

export const translationSpec = new JobSpec({
  name: "translation",
  input: {
    default: translationInputSchema.extend({
      llmType: z.enum(["openai", "ollama"]).default("ollama").optional(),
    }),
    lang: supportedLangs
  },
  output: translationOutputSchema,
});

export const translationWorker = translationSpec.defineWorker({
  processor: async ({ input, output, invoke }) => {
    let currLang: z.infer<typeof supportedLangs> = supportedLangs.Enum.French;

    const langObs = input("lang").observable();
    langObs.subscribe(newLang => {
      if(newLang) {
        currLang = newLang;
      }
    });

    // const langLoop = async () => {
    //   for await(const newLang of input("lang")) {
    //     currLang = newLang;
    //   }  
    // }
    // langLoop();
   

    for await (const data of input("default")) {
      const { llmType, text } = data;
      // obtain translation results from the LLM
      const job = await getQueuedJobOrCreate({ llmType: llmType || "ollama" });
      const { input: childInput, output: childOutput } = job;
      await childInput.feed({ text, toLang: currLang  });
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

