import { z } from "zod";
import {
  Liveflow,
  JobSpec,
  LiveEnv,
  conn,
  expose,
  getLocalTempFileStorageProvider,
} from "@livestack/core";
import { translationSpec } from "@livestack/translate-server";

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

const liveflow = Liveflow.define({
  name: "fake-liveflow",
  connections: [
    conn({
      from: helloJobSpec,
      transform: ({ greeting }) => ({ text: greeting, llmType: "openai"}),
      to: translationSpec,
    }),
  ],
  exposures: [
    expose(helloJobSpec.input.default, "input-default"),
    expose(translationSpec.input.language, "language"),
    expose(translationSpec.output.default, "output-default"),
  ],
});

if (module === require.main) {
  (async () => {
    // await liveflow.startWorker();
    const { input, output, waitUntilFinish } = await liveflow.enqueueJob({
      // jobId: Date.now().toString(),
    });
    console.info("input tags: ", input.tags);
    console.info("output tags: ", output.tags);

    await input("language").feed("French");
    await input("input-default").feed({ name: "Hello World. This is Emily" });

    for await (const { data: r, timestamp } of output("output-default")) {
      console.log(`[${timestamp}]`, r);
    }
    
    await waitUntilFinish();
    console.log("done");
    process.exit(0);
  })();
}
