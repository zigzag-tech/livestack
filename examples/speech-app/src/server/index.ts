import { LiveEnv, getLocalTempFileStorageProvider } from "@livestack/core";
import { initJobBinding } from "@livestack/gateway";
import express from "express";
import path from "path";
import bodyParser from "body-parser";
import cors from "cors";
import ViteExpress from "vite-express";
import { speechWorkflow } from "./workflow.speech";

const liveEnvP = LiveEnv.create({
  projectId: "MY_LIVE_SPEECH_APP",
  storageProvider: getLocalTempFileStorageProvider("/tmp/zzlive"),
});

// Main function
async function main() {
  // Set the global LiveEnv
  LiveEnv.setGlobal(liveEnvP);

  // Create an Express app
  const app = express();
  app.use(cors());
  app.use(bodyParser.json());
  app.use(express.static(path.join(__dirname, "..", "public")));

  // Define the port for the server
  const PORT = 4700;

  // Start the server
  const httpServer = ViteExpress.listen(app, PORT, () => {
    console.info(`Server running on http://localhost:${PORT}.`);
  });

  // Initialize job binding for any incoming requests from the client
  initJobBinding({
    httpServer,
    allowedSpecsForBinding: [speechWorkflow],
  });
}

// Call the main function
main();
