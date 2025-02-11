import shortUniqueId from "short-unique-id";

import { z } from "zod";
import { saveToJSON } from "./utils";
import { runAtLeast } from "@livestack/lab-internal-server";
import {
  Liveflow,
  JobSpec,
  LiveEnv,
  conn,
  expose,
  getLocalTempFileStorageProvider,
  sleep,
} from "@livestack/core";

async function main() {
  const newLiveEnvP = LiveEnv.create({
    projectId: "chain-dummy-test",
    storageProvider: getLocalTempFileStorageProvider("/tmp/zzlive"),
  });

  LiveEnv.setGlobal(newLiveEnvP);

  const jobGroupId = Date.now().toString() + new shortUniqueId().stamp(10);
  const fakeTextGenSpec = JobSpec.define({
    name: "fake-text-gen-spec",
    input: {
      "num-stream1": z.object({ num: z.number() }),
      "bool-stream2": z.object({ multiply: z.boolean() }),
    },
    output: z.object({ combined: z.string() }),
  });

  const fakeTextGenWorker = fakeTextGenSpec.defineWorker({
    processor: async ({ output, input }) => {
      let cumu = 0;
      let multiple = false;

      // async
      (async () => {
        for await (const data of input("bool-stream2")) {
          multiple = data.multiply;
        }
      })();

      for await (const data of input("num-stream1")) {
        cumu += data.num;
        await sleep(10);
        await output.emit({
          combined: "hello " + data.num * (multiple ? 100 : 1),
        });
      }
    },
  });

  const fakeTextSummarizerSpec = new JobSpec({
    name: "fake-text-summarizer-spec",
    jobOptions: z.object({}),
    input: z.object({ text: z.string() }),
    output: { default: z.object({ summarized: z.string() }) },
  });

  const fakeTextSummarizerWorker = fakeTextSummarizerSpec.defineWorker({
    processor: async ({ input, output }) => {
      for await (const data of input) {
        await output.emit({
          summarized: "summary: " + data.text,
        });
      }
    },
  });

  const liveflow = Liveflow.define({
    name: "fake-liveflow",
    connections: [
      conn({
        from: fakeTextGenSpec.output.default,
        transform: ({ combined }) => ({ text: combined }),
        to: fakeTextSummarizerSpec.input.default,
      }),
    ],
    exposures: [
      expose(fakeTextGenSpec.input["num-stream1"], "num"),
      expose(fakeTextGenSpec.input["bool-stream2"], "bool"),
      expose(fakeTextGenSpec.output["default"], "intermediate-text"),
      expose(fakeTextSummarizerSpec.output.default, "text"),
    ],
  });

  const { input, output, graph, waitUntilFinish } = await liveflow.enqueueJob({
    jobId: jobGroupId,
    jobOptions: [
      {
        spec: fakeTextGenSpec.name,
        params: {},
      },
      {
        spec: fakeTextSummarizerSpec.name,
        params: {},
      },
    ],
  });

  // await plotAndSaveGraph(graph);
  await saveToJSON(graph);

  (async () => {
    for (const num of Array.from({ length: 30 }, (_, i) => i)) {
      await runAtLeast(
        200,
        input("num").feed({
          num,
        })
      );
    }

    await input("num").terminate();
  })();

  let multiply = false;
  output("intermediate-text").valueObservable.subscribe((v) => {
    console.log("output intermediate-text: ", v?.data);
  });

  (async () => {
    for (const _ of Array.from({ length: 5 }, (_, i) => i)) {
      multiply = !multiply;

      await runAtLeast(
        500 + Math.random() * 500,
        input("bool").feed({
          multiply,
        })
      );
    }
    await input("bool").terminate();
  })();
  console.info("input tags: ", input.tags);
  console.info("output tags: ", output.tags);

  for await (const { data: r, timestamp } of output("text")) {
    console.log(`[${timestamp}]`, r);
  }
  await waitUntilFinish();
  console.log("done");
  await sleep(6000);

  process.exit();
}

// execute main if this script is run directly
if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}

// See it live at https://live.dev/329iffdds

// Register for a token for free: https://livestack.dev/register
// Enter token:
