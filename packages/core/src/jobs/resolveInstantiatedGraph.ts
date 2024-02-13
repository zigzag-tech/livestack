import {
  InstantiatedGraph,
  StreamIdOverridesForRootSpec,
  StreamNode,
  getSourceSpecNodeConnectedToStream,
} from "@livestack/shared";
import { vaultClient } from "@livestack/vault-client";
import { ConnectorType } from "@livestack/vault-interface";
import { TransformRegistry } from "../orchestrations/TransformRegistry";
import { lruCacheFn } from "../utils/lruCacheFn";
import { JobSpec } from "./JobSpec";
import { ZZEnv } from "./ZZEnv";

// TODO: cache this
export async function resolveInstantiatedGraph({
  zzEnv,
  spec,
  jobId,
}: {
  zzEnv: ZZEnv;
  spec: JobSpec<any, any, any, any, any>;
  jobId: string;
}): Promise<InstantiatedGraph> {
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
  const streamSourceSpecTypeByStreamId: Record<
    string,
    {
      specName: string;
      tag: string;
    }
  > = {};

  if (parentRec.rec) {
    const pRec = parentRec.rec;
    const parentSpec = JobSpec.lookupByName(pRec.spec_name);
    const parentInstaG = await resolveInstantiatedGraph({
      zzEnv,
      spec: parentSpec,
      jobId: pRec.parent_job_id,
    });

    const streamNodeIds = parentInstaG.filterNodes(
      (nId) => parentInstaG.getNodeAttributes(nId).nodeType === "stream"
    );

    for (const streamNodeId of streamNodeIds) {
      const streamId = (
        parentInstaG.getNodeAttributes(streamNodeId) as StreamNode
      ).streamId;
      const sourceSpecNode = getSourceSpecNodeConnectedToStream(
        parentInstaG,
        streamNodeId
      );
      if (sourceSpecNode) {
        streamSourceSpecTypeByStreamId[streamId] = {
          specName: sourceSpecNode.origin.specName,
          tag: sourceSpecNode.outletNode.tag,
        };
      }
    }

    for (const tag of spec.inputTags) {
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
    streamSourceSpecTypeByStreamId,
  });

  return instaG;
}

const getParentRecCached = lruCacheFn(
  (rec) => `${rec.project_id}/${rec.childJobId}`,
  vaultClient.db.getParentJobRec.bind(vaultClient.db)
);

const getJobStreamConnectorRecsCached = lruCacheFn(
  (rec) => `${rec.projectId}/${rec.jobId}`,
  vaultClient.db.getJobStreamConnectorRecs.bind(vaultClient.db)
);
