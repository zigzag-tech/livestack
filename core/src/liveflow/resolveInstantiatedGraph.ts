import {
  InstantiatedGraph,
  StreamIdOverridesForRootSpec,
  StreamNode,
  initInstantiatedGraph,
} from "@livestack/shared";
import { ConnectorType } from "@livestack/vault-interface";
import { TransformRegistry } from "./TransformRegistry";
import { lruCacheFn } from "@livestack/shared";
import { JobSpec } from "../jobs/JobSpec";
import { LiveEnv } from "../env/LiveEnv";

// TODO: cache this
export const resolveInstantiatedGraph = lruCacheFn(
  ({ specName, jobId }) => `${specName}/${jobId}`,
  async ({
    liveEnv,
    specName,
    jobId,
  }: {
    liveEnv: LiveEnv;
    specName: string;
    jobId: string;
  }): Promise<InstantiatedGraph> => {
    const spec = JobSpec.lookupByName(specName);

    if (
      getJobStreamConnectorRecsCachedPByProjectUuid[liveEnv.projectUuid] ===
      undefined
    ) {
      getJobStreamConnectorRecsCachedPByProjectUuid[liveEnv.projectUuid] =
        lruCacheFn(
          ((rec: any) => `${rec.projectUuid}/${rec.jobId}`) as any,
          liveEnv.vaultClient.db.getJobStreamConnectorRecs.bind(
            liveEnv.vaultClient.db
          )
        );
    }

    const getJobStreamConnectorRecsCachedP =
      getJobStreamConnectorRecsCachedPByProjectUuid[liveEnv.projectUuid];
    const { records: streams } = await(await getJobStreamConnectorRecsCachedP)({
      projectUuid: liveEnv.projectUuid,
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

    if (
      cacgetParentRecCachedPByProjectUuid[liveEnv.projectUuid] === undefined
    ) {
      cacgetParentRecCachedPByProjectUuid[liveEnv.projectUuid] = lruCacheFn(
        ((rec: any) => `${rec.project_uuid}/${rec.childJobId}`) as any,
        liveEnv.vaultClient.db.getParentJobRec.bind(liveEnv.vaultClient.db)
      );
    }

    const getParentRecCachedP =
      cacgetParentRecCachedPByProjectUuid[liveEnv.projectUuid];

    const parentRec = await(await getParentRecCachedP)({
      projectUuid: liveEnv.projectUuid,
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
        liveEnv,
        specName: pRec.spec_name,
        jobId: pRec.parent_job_id,
      });

      const streamNodeIds = parentInstaG.nodes().filter(
        (nId) => parentInstaG.getNodeAttributes(nId).nodeType === "stream"
      );

      for (const streamNodeId of streamNodeIds) {
        const streamId = (
          parentInstaG.getNodeAttributes(streamNodeId) as StreamNode
        ).streamId;
        const sourceSpecNode = parentInstaG.getSourceSpecNodeConnectedToStream(
          streamNodeId
        );
        if (sourceSpecNode) {
          if(!sourceSpecNode.origin.specName) {
            throw new Error("sourceSpecNode.origin.specName is null");
          }
          if(!sourceSpecNode.outletNode.tag) {
            throw new Error("sourceSpecNode.outletNode.tag is null");
          }
          streamSourceSpecTypeByStreamId[streamId] = {
            specName: sourceSpecNode.origin.specName,
            tag: sourceSpecNode.outletNode.tag,
          };
        }
      }

      for (const tag of spec.inputTags) {
        const transform = TransformRegistry.getTransform({
          liveflowSpecName: pRec.spec_name,
          receivingSpecName: spec.name,
          receivingSpecUniqueLabel: pRec.unique_spec_label || null,
          tag: tag.toString(),
        });
        if (!!transform) {
          inletHasTransformOverridesByTag[tag.toString()] = true;
        }
      }
    }

    const instaG = initInstantiatedGraph({
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

const cacgetParentRecCachedPByProjectUuid: Record<
  string,
  LiveEnv["vaultClient"]["db"]["getParentJobRec"]
> = {};
const getJobStreamConnectorRecsCachedPByProjectUuid: Record<
  string,
  LiveEnv["vaultClient"]["db"]["getJobStreamConnectorRecs"]
> = {};

