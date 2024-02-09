import { StreamIdOverridesForRootSpec } from "../orchestrations/InstantiatedGraph";
import { ZZEnv } from "./ZZEnv";
import { InstantiatedGraph } from "../orchestrations/InstantiatedGraph";
import { JobSpec } from "./JobSpec";
import { TransformRegistry } from "../orchestrations/TransformRegistry";
import { dbClient } from "@livestack/vault-client";
import { ConnectorType } from "@livestack/vault-interface";
import { lruCacheFn } from "../utils/lruCacheFn";

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
  const { records: streams } = await getJobStreamConnectorRecsCached({
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

  const parentRec = await getParentRecCached({
    projectId: zzEnv.projectId,
    childJobId: jobId,
  });

  const inletHasTransformOverridesByTag: Record<string, boolean> = {};
  if (parentRec.rec) {
    const pRec = parentRec.rec;
    for (const tag of spec.inputDefSet.keys) {
      const transform = TransformRegistry.getTransform({
        workflowSpecName: pRec.spec_name,
        receivingSpecName: spec.name,
        receivingSpecUniqueLabel: pRec.unique_spec_label || null,
        tag: tag.toString(),
      });
      if (!!transform) {
        inletHasTransformOverridesByTag[tag.toString()] = true;
      }
    }
  }

  const instaG = new InstantiatedGraph({
    defGraph: spec.getDefGraph(),
    contextId: parentRec.rec?.parent_job_id || jobId,
    rootJobId: jobId,
    streamIdOverrides,
    inletHasTransformOverridesByTag,
  });

  return instaG;
}

const getParentRecCached = lruCacheFn(
  (rec) => `${rec.project_id}/${rec.childJobId}`,
  dbClient.getParentJobRec.bind(dbClient)
);

const getJobStreamConnectorRecsCached = lruCacheFn(
  (rec) => `${rec.projectId}/${rec.jobId}`,
  dbClient.getJobStreamConnectorRecs.bind(dbClient)
);
