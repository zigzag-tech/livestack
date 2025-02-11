import { LiveEnv } from "@livestack/core";
import { getLocalTempFileStorageProvider } from "@livestack/core";

const project_id = "TEST";

export const liveEnvP = LiveEnv.create({
  projectId: project_id,
  storageProvider: getLocalTempFileStorageProvider("/tmp/zzlive"),
});
