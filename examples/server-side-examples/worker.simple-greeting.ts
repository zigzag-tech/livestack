import { z } from "zod";
import { JobSpec, LiveEnv } from "@livestack/core";
import { getLocalTempFileStorageProvider } from "@livestack/core";

const liveEnvP = LiveEnv.create({
  projectId: "EXAMPLE_PROJECT",
  storageProvider: getLocalTempFileStorageProvider("/tmp/livestack"),
});

LiveEnv.setGlobal(liveEnvP);

const TEST_JOB_ID = "t-" + Date.now();

const helloJobSpec = JobSpec.define({
  name: "dummy-test-spec",
  input: z.object({ name: z.string() }),
  output: z.object({ greeting: z.string() }),
});

const helloWorkerDef = helloJobSpec.defineWorker({
  processor: async ({ input, output }) => {
    for await (const data of input) {
      output.emit({ greeting: `Hello, ${data.name}!` });
    }
  },
});

if (module === require.main) {
  (async () => {
    const { input, output } = await helloWorkerDef.enqueueJob({
      jobId: TEST_JOB_ID,
      jobOptions: {
        startingNum: 42,
      },
    });

    const names = ["Alice", "Bob", "Charlie"];
    for (const name of names) {
      await input.feed({ name });
    }
    await input.terminate();

    for await (const data of output) {
      console.log(data.data.greeting);
    }
    console.log("done");
    process.exit(0);
  })();
}
