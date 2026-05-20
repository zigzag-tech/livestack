import { genManuallyFedIterator } from "@livestack/shared";
import {
  CacapcityServiceImplementation,
  FromWorker,
  CommandToInstance,
} from "@livestack/vault-interface";
import {
  InstanceResponseToCapacityQueryMessage,
  InstanceResponseToProvisionMessage,
  type ReportAsInstanceMessage,
  type ServerStreamingMethodResult,
  SpecNameAndCapacity,
  RespondToCapacityLogMessage,
} from "@livestack/vault-interface/src/generated/capacity.js";
import { CallContext } from "nice-grpc";
import _ from "lodash";
import { v4 } from "uuid";

class CapacityManager implements CacapcityServiceImplementation {
  

  public readonly sessionId = v4();
  constructor() {}
  private readonly requestTimeoutMs = 30_000;
  private readonly pendingCheckInFlightByProjectUuid = new Set<string>();

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
  
  // In-memory tracking of pending jobs by project and spec
  pendingJobsByProjectAndSpec: Record<string, Record<string, number>> = {};

  capacityQuery: Record<string, string[]> = {};

  instanacesByProejctUuid = {};

  setPendingJobs(projectUuid: string, specName: string, count: number) {
    if (!this.pendingJobsByProjectAndSpec[projectUuid]) {
      this.pendingJobsByProjectAndSpec[projectUuid] = {};
    }
    this.pendingJobsByProjectAndSpec[projectUuid][specName] = Math.max(
      this.pendingJobsByProjectAndSpec[projectUuid][specName] ?? 0,
      count
    );
  }
  
  // Remove a job from pending tracking (when it gets processed)
  removePendingJob(projectUuid: string, specName: string) {
    if (this.pendingJobsByProjectAndSpec[projectUuid]?.[specName] > 0) {
      this.pendingJobsByProjectAndSpec[projectUuid][specName]--;
    }
  }

  async respondToCapacityLog(
    request: RespondToCapacityLogMessage,
    context: CallContext
  ): Promise<{
    specCapacity?:
      | {
          specName?: string | undefined;
          capacity?: number | undefined;
        }[]
      | undefined;
  }> {
    const projectUuid = request.projectUuid;
    const capacitiesLog = this.logCapacity(projectUuid);
    const capacities = await capacitiesLog;
    const sumsBySpecName: Record<string, number> = {};
    for (const key in capacities) {
      if (capacities.hasOwnProperty(key)) {
        const capacitiesByInstanceId = capacities[key];
        for (const item of capacitiesByInstanceId) {
          if (sumsBySpecName[item.specName]) {
            sumsBySpecName[item.specName] += item.capacity;
          } else {
            sumsBySpecName[item.specName] = item.capacity;
          }
        }
      }
    }
    const specCapacities = Object.keys(sumsBySpecName).map((specName) => ({
      specName: specName,
      capacity: sumsBySpecName[specName],
    }));
    return { specCapacity: specCapacities };
  }

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
    // capacity log
    const { iterator: iter, resolveNext: resolveJobPromise } =
      genManuallyFedIterator<CommandToInstance>();

    const { instanceId, projectUuid } = request;
    context.signal.addEventListener("abort", (e) => {
      console.log("aborting", e);
    });

    this.resolveByInstanceId[instanceId] = resolveJobPromise;

    console.debug(
      `reportInstanceOnline from instance ${instanceId}: ${projectUuid}.`
    );

    this.instanceIdsByProjectUuid[projectUuid] = Array.from(new Set([
      ...(this.instanceIdsByProjectUuid[projectUuid] || []),
      instanceId,
    ]));

    // After a new instance comes online, check for any pending jobs
    this.checkPendingJobs(projectUuid).catch(err => {
      console.error("Error checking pending jobs after instance online:", err);
    });

    // clear all capacities on disconnect
    const abortListener = async () => {
      console.debug(
        `Instance gone: from instance ${instanceId}: ${projectUuid}.`
      );
      // capcity log

      delete this.resolveByInstanceId[instanceId];
      this.dropPendingRequestsForInstance(instanceId);

      // remove instanceId from projectUuid lookup
      this.instanceIdsByProjectUuid[projectUuid] =
        this.instanceIdsByProjectUuid[projectUuid].filter(
          (id) => id !== instanceId
        );

      console.log(`instanceIds: ${this.instanceIdsByProjectUuid[projectUuid]}`);

      context.signal.removeEventListener("abort", abortListener);
    };

    context.signal.addEventListener("abort", abortListener);

    return iter;
  }

  async logCapacity(projectUuid: string) {
    const instanceIds = this.instanceIdsByProjectUuid[projectUuid] || [];

    const capacitiesByInstanceId: Record<string, SpecNameAndCapacity[]> = {};

    const capacities = await this.queryAllLiveCapacities(projectUuid, instanceIds);

    for (const capacity of capacities) {
      capacitiesByInstanceId[capacity.instanceId] =
        capacity.specNameAndCapacity;
    }

    return capacitiesByInstanceId;
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
    const requestKey =
      `instances//${instanceId}//projects//${projectUuid}\$\$${correlationId}` as const;
    sendCmdToClient({
      projectUuid,
      instanceId,
      queryCapacity: {},
      correlationId,
    });

    const promise = new Promise<InstanceResponseToCapacityQueryMessage>(
      (resolve, reject) => {
        const timer = setTimeout(() => {
          this.pendingResponseToCapacityQueryResolveFnMap.delete(requestKey);
          reject(new Error(`Timed out waiting for capacity from ${instanceId}:${projectUuid}`));
        }, this.requestTimeoutMs);
        timer.unref?.();
        this.pendingResponseToCapacityQueryResolveFnMap.set(
          requestKey,
          (msg) => {
            clearTimeout(timer);
            this.pendingResponseToCapacityQueryResolveFnMap.delete(requestKey);
            resolve(msg);
          }
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
      console.warn(`No pending capacity response found for ${instanceId}:${projectUuid}`);
      return {};
    }
    this.pendingResponseToCapacityQueryResolveFnMap.delete(
      `instances//${instanceId}//projects//${projectUuid}\$\$${correlationId}`
    );
    resolveFn(request);
    
    // Check for pending jobs after receiving capacity information
    this.checkPendingJobs(projectUuid).catch(err => {
      console.error("Error checking pending jobs after capacity query:", err);
    });
    
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
    const requestKey =
      `instanaces//${instanceId}//projects//${projectUuid}//specs//${specName}\$\$${correlationId}` as const;
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
      (resolve, reject) => {
        const timer = setTimeout(() => {
          this.pendingResponseToProvisionResolveFnMap.delete(requestKey);
          reject(new Error(`Timed out waiting for provision from ${instanceId}:${projectUuid}:${specName}`));
        }, this.requestTimeoutMs);
        timer.unref?.();
        resolveFn = resolve;
        this.pendingResponseToProvisionResolveFnMap.set(
          requestKey,
          (msg) => {
            clearTimeout(timer);
            this.pendingResponseToProvisionResolveFnMap.delete(requestKey);
            resolveFn(msg);
          }
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
      console.warn(`No pending provision response found for ${instanceId}:${projectUuid}:${specName}`);
      return Promise.resolve({});
    }
    this.pendingResponseToProvisionResolveFnMap.delete(
      `instanaces//${instanceId}//projects//${projectUuid}//specs//${specName}\$\$${correlationId}`
    );
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

    const capacities = await this.queryAllLiveCapacities(projectUuid, instanceIds);

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
          this.forgetInstance(projectUuid, instanceId);
          continue;
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
      
      // Return early if no qualified instances found
      return;
    }

    const nextBestInstanceId = _.sample(qualifiedCapacities)!.instanceId;

    const provisionResponse = await this.runProvision({
      projectUuid,
      instanceId: nextBestInstanceId,
      specName,
      numberOfWorkersNeeded: by,
    });
    
    if ((provisionResponse.numberOfWorkersStarted ?? 0) > 0) {
      this.removePendingJob(projectUuid, specName);
    }
  }

  async checkPendingJobs(projectUuid: string): Promise<void> {
    if (this.pendingCheckInFlightByProjectUuid.has(projectUuid)) return;
    this.pendingCheckInFlightByProjectUuid.add(projectUuid);
    try {
      // Get all specs with pending jobs for this project
      const projectJobs = this.pendingJobsByProjectAndSpec[projectUuid];
      if (!projectJobs) return;
      
      for (const [specName, count] of Object.entries(projectJobs)) {
        if (count > 0) {
          console.log(`Found ${count} pending jobs for ${projectUuid}/${specName}`);
          
          // Try to increase capacity for each waiting job
          for (let i = 0; i < count; i++) {
            await this.increaseCapacity({
              projectUuid,
              specName,
              by: 1,
            });
          }
        }
      }
    } catch (error) {
      console.error("Error in checkPendingJobs:", error);
    } finally {
      this.pendingCheckInFlightByProjectUuid.delete(projectUuid);
    }
  }

  private async queryAllLiveCapacities(
    projectUuid: string,
    instanceIds: string[]
  ): Promise<InstanceResponseToCapacityQueryMessage[]> {
    const liveInstanceIds = Array.from(new Set(instanceIds)).filter((instanceId) => {
      if (this.resolveByInstanceId[instanceId]) return true;
      this.forgetInstance(projectUuid, instanceId);
      return false;
    });
    const settled = await Promise.allSettled(
      liveInstanceIds.map((instanceId) =>
        this.runCapacityQuery({ instanceId, projectUuid })
      )
    );
    return settled.flatMap((result, index) => {
      if (result.status === "fulfilled") return [result.value];
      const instanceId = liveInstanceIds[index];
      if (instanceId) this.forgetInstance(projectUuid, instanceId);
      console.warn(result.reason);
      return [];
    });
  }

  private forgetInstance(projectUuid: string, instanceId: string) {
    delete this.resolveByInstanceId[instanceId];
    this.dropPendingRequestsForInstance(instanceId);
    this.instanceIdsByProjectUuid[projectUuid] =
      (this.instanceIdsByProjectUuid[projectUuid] || []).filter(
        (id) => id !== instanceId
      );
  }

  private dropPendingRequestsForInstance(instanceId: string) {
    for (const key of this.pendingResponseToCapacityQueryResolveFnMap.keys()) {
      if (key.startsWith(`instances//${instanceId}//`)) {
        this.pendingResponseToCapacityQueryResolveFnMap.delete(key);
      }
    }
    for (const key of this.pendingResponseToProvisionResolveFnMap.keys()) {
      if (key.startsWith(`instanaces//${instanceId}//`)) {
        this.pendingResponseToProvisionResolveFnMap.delete(key);
      }
    }
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
