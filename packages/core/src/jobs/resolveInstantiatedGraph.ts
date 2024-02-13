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
export const resolveInstantiatedGraph = lruCacheFn(
  ({ specName, jobId }) => `${specName}/${jobId}`,
  async ({
    zzEnv,
    specName,
    jobId,
  }: {
    zzEnv: ZZEnv;
    specName: string;
    jobId: string;
  }): Promise<InstantiatedGraph> => {
    const spec = JobSpec.lookupByName(specName);
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
      const parentInstaG = await resolveInstantiatedGraph({
        zzEnv,
        specName: pRec.spec_name,
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
);

const getParentRecCached = lruCacheFn(
  ((rec: any) => `${rec.project_id}/${rec.childJobId}`) as any,
  vaultClient.db.getParentJobRec.bind(vaultClient.db)
);

const getJobStreamConnectorRecsCached = lruCacheFn(
  ((rec: any) => `${rec.projectId}/${rec.jobId}`) as any,
  vaultClient.db.getJobStreamConnectorRecs.bind(vaultClient.db)
);
