import { genManuallyFedIterator } from "@livestack/shared";
import {
  CacapcityServiceImplementation,
  FromInstance,
  FromWorker,
  CommandToInstance,
} from "@livestack/vault-interface";
import { ServerStreamingMethodResult } from "@livestack/vault-interface/src/generated/capacity";
import { CallContext } from "nice-grpc";
import { createClient } from "redis";

class CapacityManager implements CacapcityServiceImplementation {
  redisClient = createClient();
  constructor() {}

  resolveByInstanceId: Record<string, (value: CommandToInstance) => void> = {};

  reportAsInstance(
    request: AsyncIterable<FromInstance>,
    context: CallContext
  ): ServerStreamingMethodResult<{
    instanceId?: string | undefined;
    provision?:
      | {
          projectId?: string | undefined;
          specName?: string | undefined;
          numberOfWorkersNeeded?: number | undefined;
        }
      | undefined;
  }> {
    const { iterator: iter, resolveNext: resolveJobPromise } =
      genManuallyFedIterator<CommandToInstance>();

    (async () => {
      for await (const {
        reportCapacityAvailability,
        instanceId,
        projectId,
      } of request) {
        if (reportCapacityAvailability) {
          this.resolveByInstanceId[instanceId] = resolveJobPromise;
          const { maxCapacity } = reportCapacityAvailability;
          console.debug(
            `reportCapacityAvailability: ${instanceId} ${projectId} ${reportCapacityAvailability.specName} ${maxCapacity}`
          );
          const client = await this.redisClient.connect();
          const projectIdN = escapeColon(projectId);
          const specNameN = escapeColon(reportCapacityAvailability.specName);
          const instanceIdN = escapeColon(instanceId);
          await client.sendCommand([
            "HSET",
            `zz_maxcapacity:${projectIdN}:${specNameN}`,
            instanceIdN,
            maxCapacity.toString(),
          ]);

          await client.sendCommand([
            "SADD",
            `zz_instance:${instanceIdN}`,
            `${projectIdN}:${specNameN}`,
          ]);

          // clear all capacities on disconnect
          context.signal.onabort = async () => {
            console.debug("reportInstance disconnected.");
            // get the set of reported projectId:specName and clear capacity for each
            const porjectIdSpecNamePairs = (await client.sendCommand([
              "SMEMBERS",
              `zz_instance:${instanceIdN}`,
            ])) as `${string}:${string}`[];

            for (const pair of porjectIdSpecNamePairs) {
              const [projectIdN, specNameN] = pair.split(":");
              await client.sendCommand([
                "HDEL",
                `zz_maxcapacity:${projectIdN}:${specNameN}`,
                instanceIdN,
              ]);

              // remove capacity hash values
              await client.sendCommand([
                "HDEL",
                `zz_maxcapacity:${projectIdN}:${specNameN}`,
                instanceIdN,
              ]);
              delete this.resolveByInstanceId[instanceIdN];
            }
          };
        } else {
          throw new Error("Invalid command");
        }
      }
    })();
    return iter;
  }

  async increaseCapacity({
    projectId,
    specName,
    by,
  }: {
    projectId: string;
    specName: string;
    by: number;
  }) {
    // set the relevant capacity in redis
    const client = await this.redisClient.connect();
    const projectIdN = escapeColon(projectId);
    const specNameN = escapeColon(specName);
    // get all instances (keys) for this projectId:specName
    const instanceIdsAndMaxCapacities = (await client.sendCommand([
      "HGETALL",
      `zz_maxcapacity:${projectIdN}:${specNameN}`,
    ])) as string[];
    const maxCapacityByInstanceId = Object.fromEntries(
      instanceIdsAndMaxCapacities.map(
        (v, i) => [v, Number(instanceIdsAndMaxCapacities[i + 1])] as const
      )
    );

    // choose the instance with the most capacity
    const instanceId = Object.entries(maxCapacityByInstanceId).reduce((a, b) =>
      a[1] > b[1] ? a : b
    )[0];

    // nudge the instance to increase capacity
    const resolve = this.resolveByInstanceId[instanceId];
    if (!resolve) {
      throw new Error(`No instance found for ${projectId}:${specName}`);
    }
    resolve({
      projectId,
      instanceId,
      provision: {
        projectId,
        specName,
        numberOfWorkersNeeded: by,
      },
    });
  }
}

const _capacityManagerByProjectId: Record<string, CapacityManager> = {};

export const getCapacityManager = () => {
  if (!_capacityManagerByProjectId["default"]) {
    _capacityManagerByProjectId["default"] = new CapacityManager();
  }
  return _capacityManagerByProjectId["default"]!;
};

export function escapeColon(s: string) {
  return s.replace(":", "__colon__");
}
