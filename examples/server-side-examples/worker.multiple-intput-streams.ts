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
  input: {
    name: z.object({ name: z.string() }),
    "favorite-food": z.object({ food: z.string() }),
  },
  output: z.object({ greeting: z.string() }),
});

const helloWorkerDef = helloJobSpec.defineWorker({
  processor: async ({ input, output }) => {
    for await (const data of input.merge("name", "favorite-food")) {
      const info = {} as { name?: string; food?: string };
      if (data.tag === "name") {
        info.name = data.data.name;
      } else {
        info.food = data.data.food;
      }

      if (info.name && info.food) {
        output.emit({
          greeting: `Hello, ${info.name}! Your favorite food is ${info.food}`,
        });
      }
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
    const foods = ["Pizza", "Burger", "Pasta"];
    for (let i = 0; i < names.length; i++) {
      await input("name").feed({ name: names[i] });
      await input("favorite-food").feed({ food: foods[i] });
    }
    await input.terminate();

    for await (const data of output) {
      console.log(data.data.greeting);
    }
    console.log("done");
    process.exit(0);
  })();
}
