import {
  Liveflow,
  JobSpec,
  LiveEnv,
  conn,
  expose,
  sleep,
} from "@livestack/core";
import { z } from "zod";
import { LoremIpsum } from "lorem-ipsum";
import { saveToJSON } from "./utils";

const stringZ = z.string();

const summaryPlusHistorySchema = z.object({
  summary: z.string(),
  recentHistory: z.array(
    z.string().refine(
      (val) => {
        // Check if the string starts with "Human Player:" or "NPC:"
        return val.startsWith("Human Player:") || val.startsWith("NPC:");
      },
      {
        // Custom error message
        message: "String must start with 'Human Player:' or 'NPC:'",
      }
    )
  ),
});

const playerWorkerSpec = JobSpec.define({
  name: "PLAYER_WORKER",
  input: summaryPlusHistorySchema,
  output: stringZ,
});
const npcWorkerSpec = JobSpec.define({
  name: "NPC_WORKER",
  input: summaryPlusHistorySchema,
  output: stringZ,
});

const summarySpec = JobSpec.define({
  name: "SUMMARY_WORKER",
  input: {
    npc: stringZ,
    player: stringZ,
  },
  output: summaryPlusHistorySchema,
});

const liveflow = Liveflow.define({
  name: "CONVERSATION_LIVEFLOW",
  exposures: [
    expose(playerWorkerSpec.input.default, "player-input"),
    expose(playerWorkerSpec.output.default, "player-talk"),
    expose(npcWorkerSpec.input.default, "npc-input"),
    expose(npcWorkerSpec.output.default, "npc-talk"),
  ],

  connections: [
    conn({
      from: playerWorkerSpec,
      to: {
        spec: summarySpec,
        input: "player",
      },
    }),
    conn({
      from: npcWorkerSpec,
      to: {
        spec: summarySpec,
        input: "npc",
      },
    }),
    conn({
      from: summarySpec,
      to: playerWorkerSpec,
    }),
    conn({
      from: summarySpec,
      to: npcWorkerSpec,
    }),
  ],
});

export const playerWorker = playerWorkerSpec.defineWorker({
  processor: async ({ input, output }) => {
    for await (const { summary, recentHistory } of input) {
      console.log("PLAYER WORKER INPUT", summary, recentHistory);
      // if the last message was from the player, then the player should not respond
      if (recentHistory[recentHistory.length - 1].startsWith("Human Player")) {
        continue;
      }

      const prompt = `write anything`;
      const response = await generateResponseFake(prompt);
      if (!response.includes("quit")) {
        console.log("PLAYER WORKER RESPONSE", response);
        output.emit(response);
      }
    }
  },
});

export const npcWorker = npcWorkerSpec.defineWorker({
  processor: async ({ input, output, jobId }) => {
    for await (const { summary, recentHistory } of input) {
      console.log("NPC WORKER INPUT", summary, recentHistory);
      // if the last message was from the player, then the player should not respond
      if (recentHistory[recentHistory.length - 1].startsWith("NPC")) {
        continue;
      }

      const prompt = `write anything`;

      const response = await generateResponseFake(prompt);
      console.log("NPC WORKER RESPONSE", response);
      await output.emit(response);
    }
  },
});

export const summaryWorker = summarySpec.defineWorker({
  processor: async ({ input, output }) => {
    const recentHistory: string[] = [];

    for await (const { data, tag } of input.merge("npc", "player")) {
      const from = tag === "npc" ? "NPC" : "Human Player";
      recentHistory.push(`${from}: ${data}`);
      let prompt = "";
      let summaryOfAllThePast = "";

      if (recentHistory.length > 10) {
        const oldest = recentHistory.splice(0, 5);

        if (from === "NPC") {
          prompt = `Summarize the previous summary and the recent conversation history into a single summary.
          SUMMARY OF PAST CONVERSATION:
          ${summaryOfAllThePast}
          RECENT CONVERSATION HISTORY:
          ${oldest.join("\n")}
          
          NEW SUMMARY:
                  `;
          summaryOfAllThePast = await summarizeFakeText(oldest);
        } else {
          summaryOfAllThePast = await generateResponseFake(prompt);
        }
      }

      if (recentHistory.length > 10) {
        const oldest = recentHistory.splice(0, 5);
        summaryOfAllThePast = await summarizeFakeText(oldest);
      }

      await output.emit({
        summary: summaryOfAllThePast,
        recentHistory: recentHistory,
      });
    }
  },
});

async function summarizeFakeText(history: string[]) {
  // randomly extract words from the history and mash them up up to 20 words
  let summary = "";
  const allWords = history.join(" ").split(" ");
  for (let i = 0; i < 20; i++) {
    summary += allWords[Math.floor(Math.random() * allWords.length)] + " ";
  }
  return summary;
}

async function generateResponseFake(prompt: string) {
  await sleep(100);
  return new LoremIpsum().generateSentences(1);
}

if (require.main === module) {
  (async () => {
    LiveEnv.setGlobal(
      LiveEnv.create({
        projectId: "live-ruckus",
      })
    );

    // if (module === require.main) {
    (async () => {
      await playerWorker.startWorker();
      await npcWorker.startWorker();
      await liveflow.startWorker();

      // feed input to the playerWorker, playerWorker's output as input to npcWorker
      const initialInput: z.infer<typeof summaryPlusHistorySchema> = {
        summary: "This is the ancient dark times.",
        recentHistory: ["NPC: yello."],
      };

      const { input, output, graph } = await liveflow.enqueueJob({});
      // await plotAndSaveGraph(graph);
      await saveToJSON(graph);
      await input("player-input").feed(initialInput);

      (async () => {
        for await (const data of output("player-talk")) {
          console.log("player:", data.data);
        }
      })();
      (async () => {
        for await (const data of output("npc-talk")) {
          console.log("npc:", data.data);
        }
      })();

      console.log("done");

      // process.exit(0);
    })();
    // }
  })();
}
