import { Liveflow, LiveEnv, conn, expose } from "@livestack/core";
import express from "express";
import ViteExpress from "vite-express";
import { initJobBinding } from "@livestack/gateway";
import { getRAGWorkerDefs } from "../common/kitchen-sink-module/rag";
import { getLocalTempFileStorageProvider } from "@livestack/core";
import { getResetIndexWorkerDefs } from "../common/kitchen-sink-module/resetIndex";
import { getTTSAudio } from "../common/kitchen-sink-module/speech";
import {
  rawPCMToWavSpec,
  speechChunkToTextSpec,
} from "@livestack/transcribe/server";
import { textSplittingSpec } from "@livestack/lab-internal-server";
import { titleSummarizerSepc } from "@livestack/summarizer/server";
import {
  historySummaryJobSpec,
  historyTrackerJobSpec,
} from "@livestack/summarizer/server";

async function main() {
  const app = express();

  const liveEnv = await LiveEnv.create({
    projectId: `KITCHEN-SINK-PROJECT`,
    storageProvider: getLocalTempFileStorageProvider("/tmp/kitchen-sink"),
  });
  LiveEnv.setGlobal(liveEnv);

  const { indexJobSpec, queryJobSpec } = getRAGWorkerDefs();
  const { resetIndexJobSpec } = getResetIndexWorkerDefs();

  const transcriptionLiveflow = Liveflow.define({
    name: "transcription-index-liveflow",
    connections: [
      conn({
        from: rawPCMToWavSpec,
        transform: ({ wavb64Str }) => ({ wavb64Str, whisperType: "openai" }),
        to: speechChunkToTextSpec,
      }),
      conn({
        from: speechChunkToTextSpec,
        transform: async ({ transcript }) => transcript,
        to: textSplittingSpec,
      }),
      conn({
        from: textSplittingSpec,
        transform: (chunkText) => ({
          transcript: chunkText,
          llmType: "openai",
        }),
        to: titleSummarizerSepc,
      }),
      conn({
        from: textSplittingSpec,
        transform: (chunkText) => ({
          content: chunkText,
          source: "audio-stream",
        }),
        to: indexJobSpec,
      }),
      conn({
        from: textSplittingSpec.output.default,
        transform: async (chunkText) => ({ text: chunkText, id: 0 }), // dummy id
        to: historyTrackerJobSpec.input.default,
      }),
      conn({
        from: historyTrackerJobSpec.output["trigger-summary"],
        to: historySummaryJobSpec.input.default,
      }),
    ],
    exposures: [
      expose(rawPCMToWavSpec.input.default, "input-default"),
      expose(speechChunkToTextSpec.output.default, "transcription"),
      expose(titleSummarizerSepc.output.default, "summarized-title"),
      expose(textSplittingSpec.output["splitting-stats"], "split-stats"),
      expose(historySummaryJobSpec.output.default, "output-topics"),
    ],
  });

  const textIndexLiveflow = Liveflow.define({
    name: "text-index-liveflow",
    connections: [
      conn({
        from: indexJobSpec.output.default,
        transform: async (chunkText) => ({ text: chunkText, id: 0 }), // dummy id
        to: historyTrackerJobSpec.input.default,
      }),
      conn({
        from: indexJobSpec.output["text-status"],
        transform: () => ({ minLevel: 0 }),
        to: historySummaryJobSpec.input.default,
      }),
    ],
    exposures: [
      expose(indexJobSpec.input.default, "default"),
      expose(indexJobSpec.output["text-status"], "text-status"),
      expose(historySummaryJobSpec.output.default, "output-topics"),
    ],
  });

  app.get("/api/stream-audio", async (req, res) => {
    const response = await getTTSAudio(req.query.text as string);
    res.set({
      "Content-Type": "audio/mpeg",
      "Transfer-Encoding": "chunked",
    });
    response.data.pipe(res);
  });

  const PORT = 3000;
  const server = ViteExpress.listen(app, PORT, () =>
    console.log(`RAG Chatbot server listening on http://localhost:${PORT}.`)
  );

  initJobBinding({
    liveEnv,
    httpServer: server,
    allowedSpecsForBinding: [
      indexJobSpec,
      queryJobSpec,
      transcriptionLiveflow,
      resetIndexJobSpec,
      textIndexLiveflow,
    ],
  });
}

if (require.main === module) {
  main();
}
