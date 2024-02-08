import { StreamIdOverridesForRootSpec } from "../orchestrations/InstantiatedGraph";
import { getParentJobRec } from "@livestack/vault-dev-server/src/db/job_relations";
import { ZZEnv } from "./ZZEnv";
import { InstantiatedGraph } from "../orchestrations/InstantiatedGraph";
import { JobSpec } from "./JobSpec";
import { TransformRegistry } from "../orchestrations/TransformRegistry";
import { dbClient } from "@livestack/vault-client";
import { ConnectorType } from "@livestack/vault-interface";

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
  const { records: streams } = await dbClient.getJobStreamConnectorRecs({
    projectId: zzEnv.projectId,
    jobId,
  });

  const streamIdOverrides: StreamIdOverridesForRootSpec = Object.fromEntries(
    streams
      .map((s) => ({
        ...s,
        connector_type: s.connector_type === ConnectorType.IN ? "in" : "out",
      }))
      .map((s) => {
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
