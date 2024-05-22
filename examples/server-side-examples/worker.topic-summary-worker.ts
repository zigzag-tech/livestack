import shortUniqueId from "short-unique-id";
import { LiveEnv, getLocalTempFileStorageProvider, sleep } from "@livestack/core";
import {
  historySummaryJobSpec,
  historySummaryWorkerDef,
  historyTrackerJobSpec,
  historyTrackerWorkerDef,
} from "@livestack/summarizer/server";
import readline from "readline";
import fs from "fs";
import path from "path";

const liveEnv = LiveEnv.create({
  projectId: "dummy-test-chain",
  storageProvider: getLocalTempFileStorageProvider("/tmp/livestack"),
});

// generator function to read lines from txt file iteratively
async function* readLines() {
  const rl = readline.createInterface({
    input: fs.createReadStream(path.join(__dirname, "testLongTranscript.txt")),
    crlfDelay: Infinity,
  });
  for await (const line of rl) {
    yield line;
  }
}

LiveEnv.setGlobal(liveEnv);

async function main() {
  await historyTrackerWorkerDef.startWorker();
  await historySummaryWorkerDef.startWorker();
  const { input: trackerInput, output: trackerOutput } =
    await historyTrackerJobSpec.enqueueJob({
      jobId: new shortUniqueId().stamp(10),
    });
  const { input: summaryInput, output: summaryOutput } =
    await historySummaryJobSpec.enqueueJob({
      jobId: new shortUniqueId().stamp(10),
    });

  (async () => {
    let i = 0;
    for await (const line of readLines()) {
      await trackerInput.feed({ text: line, id: i++ });

      if (i % 100 === 0) {
        console.log(i);
        await summaryInput.feed({ minLevel: 0 });
      }
    }

    // summarize again at the end
    await summaryInput.feed({ minLevel: 0 });

    // terminate the inputs
    await trackerInput.terminate();
    await summaryInput.terminate();
  })();

  //   (async () => {
  //     for await (const data of trackerOutput) {
  //       console.log("historyTracker output", data);
  //     }
  //   })();

  for await (const data of summaryOutput) {
    console.log("historySummary output", data.data.topics);
  }
  console.log("done");
  process.exit();
}

if (module === require.main) {
  main();
}
