import { LiveEnv } from "@livestack/core";
import { getLocalTempFileStorageProvider } from "@livestack/core";
import {} from "@livestack/core";
import {
  langchainChatModelWorkerDef,
  langchainEmbeddingsWorkerDef,
} from "@common/kitchen-sink-module/provisionedLangChainModel";

async function main() {
  const liveEnv = await LiveEnv.create({
    projectId: `KITCHEN-SINK-PROJECT`,
    storageProvider: getLocalTempFileStorageProvider("/tmp/kitchen-sink"),
  });
  LiveEnv.setGlobal(liveEnv);
  await langchainChatModelWorkerDef.startWorker();
  await langchainEmbeddingsWorkerDef.startWorker();
  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

if (require.main === module) {
  main();
}
