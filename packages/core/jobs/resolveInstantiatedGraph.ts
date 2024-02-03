import { StreamIdOverridesForRootSpec } from "../orchestrations/Graph";
import { getJobStreamConnectorRecs, getParentJobRec } from "../db/knexConn";
import { ZZEnv } from "./ZZEnv";
import { InstantiatedGraph } from "../orchestrations/Graph";
import { ZZJobSpec } from "./ZZJobSpec";

export async function resolveInstantiatedGraph({
  zzEnv,
  spec,
  jobId,
}: {
  zzEnv: ZZEnv;
  spec: ZZJobSpec<any, any, any, any, any>;
  jobId: string;
}) {
  const streams = await getJobStreamConnectorRecs({
    projectId: zzEnv.projectId,
    jobId,
    dbConn: zzEnv.db,
  });

  const streamIdOverrides: StreamIdOverridesForRootSpec = Object.fromEntries(
    streams.map((s) => {
      return [`${s.connector_type}/${s.key}`, s.stream_id];
    })
  );

  const parentRec = await getParentJobRec({
    projectId: zzEnv.projectId,
    childJobId: jobId,
    dbConn: zzEnv.db,
  });

  const instaG = new InstantiatedGraph({
    defGraph: spec.getDefGraph(),
    contextId: parentRec?.job_id || jobId,
    streamIdOverrides,
  });

  return instaG;
}
