import { summaryFewShotPromptMessages } from "./prompts";
import { topicsSchema, trackedHistorySchema } from "../common/defs";
import { z } from "zod";
import { JobSpec } from "@livestack/core";
import { Message } from "ollama";
import { fewShotExamples, summarize } from "./utils";
// How this works:
// {
//  "0": [{"text": "This is a test", "ids": [8]}],
//  "1": [{"text": "This is a summary", "ids": [1, 2, 3, 4]}, {"text": "This is a summary", "ids": [5, 6, 7]}],
//  "2": [],
// }
// Summary request coming in (minLevel = 0)
// => summarize(ids[8]) + existing summary of ids[1, 2, 3, 4] + exising summary of ids[5, 6, 7]
// => generate topics

type MemoryStoreType = z.infer<typeof trackedHistorySchema>;
const memoryStore: MemoryStoreType = {};
const maxContextWindow = 10 * 1000;
const historySummaryInput = z.object({ minLevel: z.number() });

export const historyTrackerJobSpec = new JobSpec({
  name: "history-tracker",
  input: z.object({
    text: z.string(),
    id: z.number(),
  }),
  output: {
    default: trackedHistorySchema,
    "trigger-summary": historySummaryInput,
  },
});

export const historySummaryJobSpec = new JobSpec({
  name: "history-summary",
  input: historySummaryInput,
  output: topicsSchema,
});

export const historyTrackerWorkerDef = historyTrackerJobSpec.defineWorker({
  autostartWorker: false,
  processor: async ({ output, input }) => {
    let counter = 0; // used for triggering historySummaryJobSpec
    for await (const { text } of input) {
      counter += 1;
      const id = counter;

      let currLevel = 0;
      if (`${currLevel}` in memoryStore) {
        memoryStore[`${currLevel}`].push({ text, ids: [id] });
      } else {
        memoryStore[`${currLevel}`] = [{ text, ids: [id] }];
      }

      while (getCurrLevelTextsLength(currLevel) > maxContextWindow) {
        const maxIndex =
          findMaxConcatenatedText(memoryStore[`${currLevel}`]) + 1;
        const objsToSummarize = memoryStore[`${currLevel}`].slice(0, maxIndex);
        memoryStore[`${currLevel}`] =
          memoryStore[`${currLevel}`].slice(maxIndex);

        const prompt = summaryFewShotPromptMessages(
          `${objsToSummarize.map((item) => item.text).join(" ")}`
        );

        const summary = await summarize({
          useCloudSummarizer: false,
          messages: prompt,
        });
        if (!summary) {
          throw new Error("Failed to generate summary");
        }
        currLevel += 1;
        if (`${currLevel}` in memoryStore) {
          memoryStore[`${currLevel}`].push({
            text: summary,
            ids: objsToSummarize.flatMap((item) => item.ids),
          });
        } else {
          memoryStore[`${currLevel}`] = [
            { text, ids: objsToSummarize.flatMap((item) => item.ids) },
          ];
        }
        // console.log(JSON.stringify(memoryStore));
      }
      await output("default").emit(memoryStore);

      if (counter % 1 === 0) {
        // console.log("triggering summary");
        output("trigger-summary").emit({ minLevel: 0 });
      }
    }
  },
});

export const historySummaryWorkerDef = historySummaryJobSpec.defineWorker({
  autostartWorker: false,
  processor: async ({ output, input }) => {
    for await (const { minLevel } of input) {
      const baseLevels = Array.from(
        { length: minLevel + 1 },
        (_, index) => index
      );
      const baseObjsToSummarize = baseLevels.flatMap(
        (lvl) => memoryStore[`${lvl}`]
      );
      let prompt = summaryFewShotPromptMessages(
        `${baseObjsToSummarize.map((item) => item.text).join(" ")}`
      );
      let summary = await summarize({
        useCloudSummarizer: false,
        messages: prompt,
      });

      console.log(Object.keys(memoryStore));
      const maxLevel = Object.keys(memoryStore).length - 1;
      let currLevel = minLevel + 1;
      while (currLevel <= maxLevel) {
        prompt = summaryFewShotPromptMessages(
          `${summary} ${memoryStore[`${currLevel}`]
            .map((item) => item.text)
            .join(" ")}`
        );
        summary = await summarize({
          useCloudSummarizer: false,
          messages: prompt,
        });
        currLevel += 1;
      }
      const messages: Message[] = [
        ...fewShotExamples,
        {
          role: "user",
          content: `
CONTENT:
\`\`\`
${summary}
\`\`\`
`,
        },
      ];
      const topicsRaw = await summarize({
        useCloudSummarizer: false,
        messages,
      });

      const { topics } = JSON.parse(topicsRaw) as { topics: string[] };
      output.emit({
        topics,
      });
    }
  },
});

const getCurrLevelTextsLength = (currLevel: number) =>
  memoryStore[`${currLevel}`].map((item) => item.text).join(" ").length;

function findMaxConcatenatedText(objects: { text: string }[]) {
  let maxIndex = 0;
  let currentLength = 0;
  for (let i = 0; i < objects.length; i++) {
    currentLength += objects[i].text.length;
    if (currentLength <= maxContextWindow) {
      maxIndex = i;
    } else {
      break;
    }
  }
  return maxIndex;
}
