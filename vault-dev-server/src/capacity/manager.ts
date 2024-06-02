import pkg from "@livestack/shared";
const { genManuallyFedIterator } = pkg;
import {
  CacapcityServiceImplementation,
  CommandToInstance,
} from "@livestack/vault-interface";
import {
  InstanceResponseToCapacityQueryMessage,
  InstanceResponseToProvisionMessage,
  type ReportAsInstanceMessage,
  type ServerStreamingMethodResult,
} from "@livestack/vault-interface/src/generated/capacity.js";
import { CallContext } from "nice-grpc";
import { createClient } from "redis";
import _ from "lodash";
import { v4 } from "uuid";

class CapacityManager implements CacapcityServiceImplementation {
  private redisClientP = createClient().connect();
  public readonly sessionId = v4();
  constructor() {}

  resolveByInstanceId: Record<string, (value: CommandToInstance) => void> = {};
  pendingResponseToCapacityQueryResolveFnMap: Map<
    `instances//${string}//projects//${string}\$\$${string}`,
    (msg: InstanceResponseToCapacityQueryMessage) => void
  > = new Map();
  pendingResponseToProvisionResolveFnMap: Map<
    `instanaces//${string}//projects//${string}//specs//${string}\$\$${string}`,
    (msg: InstanceResponseToProvisionMessage) => void
  > = new Map();

  instanceIdsByProjectUuid: Record<string, string[]> = {};

  reportAsInstance(
    request: ReportAsInstanceMessage,
    context: CallContext
  ): ServerStreamingMethodResult<{
    projectUuid?: string | undefined;
    instanceId?: string | undefined;
    provision?:
      | {
          specName?: string | undefined;
          numberOfWorkersNeeded?: number | undefined;
        }
      | undefined;
    queryCapacity?: {} | undefined;
  }> {
    const { iterator: iter, resolveNext: resolveJobPromise } =
      genManuallyFedIterator<CommandToInstance>();

    const { instanceId, projectUuid } = request;
    context.signal.addEventListener("abort", (e) => {
      console.log("aborting", e);
    });

    const existing = this.resolveByInstanceId[instanceId];
    if (!existing) {
      this.resolveByInstanceId[instanceId] = resolveJobPromise;
    }

    console.debug(
      `reportInstanceOnline from instance ${instanceId}: ${projectUuid}.`
    );

    this.instanceIdsByProjectUuid[projectUuid] =
      this.instanceIdsByProjectUuid[projectUuid] || [];

    this.instanceIdsByProjectUuid[projectUuid].push(instanceId);

    // clear all capacities on disconnect
    const abortListener = async () => {
      console.debug(
        `Instance gone: from instance ${instanceId}: ${projectUuid}.`
      );
      delete this.resolveByInstanceId[instanceId];

      // remove instanceId from projectUuid lookup
      this.instanceIdsByProjectUuid[projectUuid] =
        this.instanceIdsByProjectUuid[projectUuid].filter(
          (id) => id !== instanceId
        );

      context.signal.removeEventListener("abort", abortListener);
    };

    context.signal.addEventListener("abort", abortListener);

    return iter;
  }

  async runCapacityQuery({
    instanceId,
    projectUuid,
  }: {
    instanceId: string;
    projectUuid: string;
  }): Promise<InstanceResponseToCapacityQueryMessage> {
    // add a resolve function for this instanceId

    const sendCmdToClient = this.resolveByInstanceId[instanceId];
    if (!sendCmdToClient) {
      throw new Error(`No instance found for ${instanceId}`);
    }
    const correlationId = v4();
    sendCmdToClient({
      projectUuid,
      instanceId,
      queryCapacity: {},
      correlationId,
    });

    const promise = new Promise<InstanceResponseToCapacityQueryMessage>(
      (resolve) => {
        this.pendingResponseToCapacityQueryResolveFnMap.set(
          `instances//${instanceId}//projects//${projectUuid}\$\$${correlationId}`,
          resolve
        );
      }
    );

    return await promise;
  }

  async respondToCapacityQuery(
    request: InstanceResponseToCapacityQueryMessage,
    context: CallContext
  ): Promise<{}> {
    const { instanceId, projectUuid, correlationId } = request;
    // console.info("respondToCapacityQuery received", request);
    const resolveFn = this.pendingResponseToCapacityQueryResolveFnMap.get(
      `instances//${instanceId}//projects//${projectUuid}\$\$${correlationId}`
    );
    if (!resolveFn) {
      throw new Error(
        `No pending response found for ${instanceId}:${projectUuid}`
      );
    }
    resolveFn(request);
    return {};
  }

  async runProvision({
    projectUuid,
    instanceId,
    specName,
    numberOfWorkersNeeded,
  }: {
    projectUuid: string;
    instanceId: string;
    specName: string;
    numberOfWorkersNeeded: number;
  }): Promise<InstanceResponseToProvisionMessage> {
    // add a resolve function for this instanceId
    const sendCmdToClient = this.resolveByInstanceId[instanceId];
    if (!sendCmdToClient) {
      throw new Error(`No instance found for ${instanceId}`);
    }
    const correlationId = v4();
    sendCmdToClient({
      projectUuid,
      instanceId,
      correlationId,
      provision: {
        specName,
        numberOfWorkersNeeded,
      },
    });
    let resolveFn: (msg: InstanceResponseToProvisionMessage) => void;

    const promise = new Promise<InstanceResponseToProvisionMessage>(
      (resolve) => {
        resolveFn = resolve;
        this.pendingResponseToProvisionResolveFnMap.set(
          `instanaces//${instanceId}//projects//${projectUuid}//specs//${specName}\$\$${correlationId}`,
          resolveFn
        );
      }
    );

    return await promise;
  }

  respondToProvision(
    request: InstanceResponseToProvisionMessage,
    context: CallContext
  ): Promise<{}> {
    const { instanceId, projectUuid, specName, correlationId } = request;
    const resolveFn = this.pendingResponseToProvisionResolveFnMap.get(
      `instanaces//${instanceId}//projects//${projectUuid}//specs//${specName}\$\$${correlationId}`
    );
    if (!resolveFn) {
      throw new Error(
        `No pending response found for ${instanceId}:${projectUuid}:${specName}`
      );
    }
    resolveFn(request);
    return Promise.resolve({});
  }

  async increaseCapacity({
    projectUuid,
    specName,
    by,
  }: {
    projectUuid: string;
    specName: string;
    by: number;
  }) {
    const instanceIds = this.instanceIdsByProjectUuid[projectUuid] || [];
    const capacities = await Promise.all(
      instanceIds.map((instanceId) =>
        this.runCapacityQuery({ instanceId, projectUuid })
      )
    );
    // console.debug("capacities", capacities);

    const qualifiedCapacities = capacities.filter((c) =>
      c.specNameAndCapacity.some(
        (s) => s.specName === specName && s.capacity >= by
      )
    );
    if (qualifiedCapacities.length === 0) {
      console.warn(
        `No instances with enough capacity found for ${projectUuid}:${specName}`
      );

      for (const instanceId of instanceIds) {
        const sendCmdToClient = this.resolveByInstanceId[instanceId];
        if (!sendCmdToClient) {
          throw new Error(`No instance found for ${instanceId}`);
        }
        const correlationId = v4();
        sendCmdToClient({
          projectUuid,
          instanceId,
          noCapacityWarning: {
            specName,
          },
          correlationId,
        });
      }
      return;
    }

    const nextBestInstanceId = _.sample(qualifiedCapacities)!.instanceId;

    await this.runProvision({
      projectUuid,
      instanceId: nextBestInstanceId,
      specName,
      numberOfWorkersNeeded: by,
    });

  }
}

const _capacityManagerByProjectUuid: Record<string, CapacityManager> = {};

export const getCapacityManager = () => {
  if (!_capacityManagerByProjectUuid["default"]) {
    _capacityManagerByProjectUuid["default"] = new CapacityManager();
  }
  return _capacityManagerByProjectUuid["default"]!;
};

export function escapeColon(s: string) {
  return s.replace(":", "__colon__");
}
