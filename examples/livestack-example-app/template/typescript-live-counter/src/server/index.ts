import { LiveEnv, JobSpec } from "@livestack/core";
import express from "express";
import ViteExpress from "vite-express";
import { INCREMENTER, incrementInput, incrementOutput } from "../common/defs";
import { initJobBinding } from "@livestack/gateway";
import bodyParser from "body-parser";

const liveEnvP = LiveEnv.create({
  projectId: "MY_LIVE_SPEECH_APP",
});

const incrementSpec = JobSpec.define({
  name: INCREMENTER,
  input: incrementInput,
  output: incrementOutput,
});

const incrementWorker = incrementSpec.defineWorker({
  processor: async ({ input, output }) => {
    let counter = 0;
    for await (const _ of input) {
      counter += 1;
      await output.emit({
        count: counter,
      });
    }
  },
});

async function main() {
  LiveEnv.setGlobal(liveEnvP);

  const app = express();
  app.use(bodyParser.json());
  const PORT = 3000;
  const server = ViteExpress.listen(app, PORT, () =>
    console.log(`Live counter server listening on http://localhost:${PORT}.`)
  );

  initJobBinding({
    httpServer: server,
    allowedSpecsForBinding: [incrementSpec],
  });
}

if (require.main === module) {
  main();
}
