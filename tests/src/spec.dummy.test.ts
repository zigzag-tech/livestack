import { z } from "zod";
import { JobSpec, LiveEnv } from "@livestack/core";
import { liveEnvP } from "./liveEnv";
import { runAtLeast } from "./utils";

LiveEnv.setGlobal(liveEnvP);

const TEST_JOB_ID = "t-" + Date.now();

const sayMeaningJobSpec = JobSpec.define({
  name: "dummy-test-spec",
  jobOptions: z.object({
    startingNum: z.number(),
  }),
  input: z.object({ hello: z.literal("world") }),
  output: {
    "meaning-of-life": z.object({ means: z.number() }),
    truth: z.object({ multiply: z.boolean() }),
  },
});

const dummyTestWorkerDef = sayMeaningJobSpec.defineWorker({
  processor: async ({ jobOptions: { startingNum }, output }) => {
    for (let i = 0; i < 30; i++) {
      await runAtLeast(
        100,
        output("meaning-of-life").emit({
          means: startingNum + i,
        })
      );
    }
  },
});

if (module === require.main) {
  (async () => {
    await dummyTestWorkerDef.startWorker({});
    const { output } = await sayMeaningJobSpec.enqueueJob({
      jobId: TEST_JOB_ID,
      jobOptions: {
        startingNum: 42,
      },
    });

    for await (const data of output.byTag("meaning-of-life")) {
      console.info("data.data.means", data.data.means);
    }
    console.info("done");

    process.exit(0);
  })();
}
