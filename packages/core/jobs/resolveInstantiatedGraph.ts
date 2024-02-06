import { StreamIdOverridesForRootSpec } from "../orchestrations/InstantiatedGraph";
import { getJobStreamConnectorRecs, getParentJobRec } from "../db/knexConn";
import { ZZEnv } from "./ZZEnv";
import { InstantiatedGraph } from "../orchestrations/InstantiatedGraph";
import { JobSpec } from "./JobSpec";
import { TransformRegistry } from "../orchestrations/TransformRegistry";

// TODO: cache this
export async function resolveInstantiatedGraph({
  zzEnv,
  spec,
  jobId,
}: {
  zzEnv: ZZEnv;
  spec: JobSpec<any, any, any, any, any>;
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

  const inletHasTransformOverridesByTag: Record<string, boolean> = {};
  if (parentRec) {
    for (const tag of spec.inputDefSet.keys) {
      const transform = TransformRegistry.getTransform({
        workflowSpecName: parentRec.spec_name,
        receivingSpecName: spec.name,
        receivingSpecUniqueLabel: parentRec.unique_spec_label || null,
        tag: tag.toString(),
      });
      if (!!transform) {
        inletHasTransformOverridesByTag[tag.toString()] = true;
      }
    }
  }

  const instaG = new InstantiatedGraph({
    defGraph: spec.getDefGraph(),
    contextId: parentRec?.job_id || jobId,
    rootJobId: jobId,
    streamIdOverrides,
    inletHasTransformOverridesByTag,
  });

  return instaG;
}
