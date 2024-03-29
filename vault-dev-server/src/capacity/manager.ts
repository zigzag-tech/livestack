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
  private redisClientP = createClient().connect();
  constructor() {}

  resolveByInstanceIAndSpecName: Record<
    string,
    Record<string, (value: CommandToInstance) => void>
  > = {};

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
        reportSpecAvailability,
        instanceId,
        projectId,
      } of request) {
        if (reportSpecAvailability) {
          const existing = this.resolveByInstanceIAndSpecName[instanceId];
          if (!existing) {
            this.resolveByInstanceIAndSpecName[instanceId] = {};
          }
          this.resolveByInstanceIAndSpecName[instanceId][
            reportSpecAvailability.specName
          ] = resolveJobPromise;
          const { maxCapacity } = reportSpecAvailability;
          console.debug(
            `reportSpecAvailability from instance ${instanceId}: ${projectId} ${reportSpecAvailability.specName} ${maxCapacity}`
          );

          const client = await this.redisClientP;
          const projectIdN = escapeColon(projectId);
          const specNameN = escapeColon(reportSpecAvailability.specName);
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
          const abortListener = async () => {
            console.debug(
              `Capacity gone: from instance ${instanceId}: ${projectId} ${reportSpecAvailability.specName} ${maxCapacity}`
            );
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
              delete this.resolveByInstanceIAndSpecName[instanceIdN][specNameN];
              context.signal.removeEventListener("abort", abortListener);
            }
            context.signal.addEventListener("abort", abortListener);
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
    const client = await this.redisClientP;
    const projectIdN = escapeColon(projectId);
    const specNameN = escapeColon(specName);

    let instanceIdsAndMaxCapacities: string[] = [];

    // keep checking until we find an instance with capacity
    while (true) {
      // get all instances (keys) for this projectId:specName
      instanceIdsAndMaxCapacities = (await client.sendCommand([
        "HGETALL",
        `zz_maxcapacity:${projectIdN}:${specNameN}`,
      ])) as string[];
      if (instanceIdsAndMaxCapacities.length === 0) {
        console.warn(
          `No instances found for ${projectId}:${specName}. Will retry in 200ms.`
        );
        await new Promise((r) => setTimeout(r, 200));
        continue;
      } else {
        break;
      }
    }

    const maxCapacitiesByInstanceId = Object.fromEntries(
      instanceIdsAndMaxCapacities.map(
        (v, i) => [v, Number(instanceIdsAndMaxCapacities[i + 1])] as const
      )
    );

    // choose the instance with the most capacity
    const instanceId = Object.entries(maxCapacitiesByInstanceId).reduce(
      (a, b) => (a[1] > b[1] ? a : b)
    )[0];

    console.log("Chosen instanceId: ", instanceId);

    // nudge the instance to increase capacity
    const resolve = this.resolveByInstanceIAndSpecName[instanceId][specName];
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
