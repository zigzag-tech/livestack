import { z } from "zod";
import {
  Liveflow,
  JobSpec,
  LiveEnv,
  conn,
  expose,
  getLocalTempFileStorageProvider,
} from "@livestack/core";
import { sleep } from "@livestack/core";
import { runAtLeast } from "@livestack/lab-internal-server";

const newLiveEnvP = LiveEnv.create({
  projectId: "dummy-test-chain",
  storageProvider: getLocalTempFileStorageProvider("/tmp/livestack"),
});

LiveEnv.setGlobal(newLiveEnvP);

const calculationSpec = JobSpec.define({
  name: "calculation-spec",
  input: {
    "number-stream": z.object({ num: z.number() }),
    "boolean-stream": z.object({ multiply: z.boolean() }),
  },
  output: z.object({ combined: z.string() }),
});

const calculationWorker = calculationSpec.defineWorker({
  processor: async ({ output, input }) => {
    let cumulative = 0;
    let multiply = false;

    (async () => {
      for await (const data of input("boolean-stream")) {
        multiply = data.multiply;
      }
    })();

    for await (const data of input("number-stream")) {
      cumulative += data.num;
      await sleep(10);
      await output.emit({
        combined: "Combine result: " + data.num * (multiply ? 100 : 1),
      });
    }
  },
});

const summarySpec = new JobSpec({
  name: "summary-spec",
  input: z.object({ text: z.string() }),
  output: { default: z.object({ summarized: z.string() }) },
});

const summaryWorker = summarySpec.defineWorker({
  processor: async ({ input, output }) => {
    for await (const data of input) {
      await output.emit({
        summarized: "Summary: " + data.text,
      });
    }
  },
});

const liveflow = Liveflow.define({
  name: "fake-liveflow",
  connections: [
    conn({
      from: calculationSpec,
      transform: ({ combined }) => ({ text: combined }),
      to: summarySpec,
    }),
  ],
  exposures: [
    expose(calculationSpec.input["number-stream"], "num"),
    expose(calculationSpec.input["boolean-stream"], "bool"),
    expose(calculationSpec.output.default, "intermediate-text"),
    expose(summarySpec.output.default, "final-output"),
  ],
});

if (module === require.main) {
  (async () => {
    const { input, output, waitUntilFinish } = await liveflow.enqueueJob({
      jobId: Date.now().toString(),
    });
    console.info("input tags: ", input.tags);
    console.info("output tags: ", output.tags);

    (async () => {
      for (const num of Array.from({ length: 10 }, (_, i) => i)) {
        await runAtLeast(
          20,
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
          200,
          input("bool").feed({
            multiply,
          })
        );
      }
      await input("bool").terminate();
    })();

    for await (const { data: r, timestamp } of output("final-output")) {
      console.log(`[${timestamp}]`, r);
    }
    
    await waitUntilFinish();
    console.log("done");
    process.exit(0);
  })();
}
