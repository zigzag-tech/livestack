import pkg from "@livestack/shared";
const { genManuallyFedIterator } = pkg;
import {
  CacapcityServiceImplementation,
  FromInstance,
  FromWorker,
  CommandToInstance,
} from "@livestack/vault-interface";
import { ServerStreamingMethodResult } from "@livestack/vault-interface/src/generated/capacity.js";
import { CallContext } from "nice-grpc";
import { createClient } from "redis";
import _ from "lodash";

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
          projectUuid?: string | undefined;
          specName?: string | undefined;
          numberOfWorkersNeeded?: number | undefined;
        }
      | undefined;
  }> {
    const { iterator: iter, resolveNext: resolveJobPromise } =
      genManuallyFedIterator<CommandToInstance>();

    (async () => {
      let knownInstanceId: string | null = null;
      let knownProjectUuid: string | null = null;

      outer: for await (const {
        reportSpecAvailability,
        instanceId,
        projectUuid,
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
            `reportSpecAvailability from instance ${instanceId}: ${projectUuid} ${reportSpecAvailability.specName} ${maxCapacity}`
          );

          const client = await this.redisClientP;
          const projectUuidN = escapeColon(projectUuid);
          const specNameN = escapeColon(reportSpecAvailability.specName);
          const instanceIdN = escapeColon(instanceId);
          await client.sendCommand([
            "HSET",
            `zz_maxcapacity:${projectUuidN}:${specNameN}`,
            instanceIdN,
            maxCapacity.toString(),
          ]);

          await client.sendCommand([
            "SADD",
            `zz_instance:${instanceIdN}`,
            `${projectUuidN}:${specNameN}`,
          ]);

          // clear all capacities on disconnect
          const abortListener = async () => {
            console.debug(
              `Capacity gone: from instance ${instanceId}: ${projectUuid} ${reportSpecAvailability.specName} ${maxCapacity}`
            );
            // get the set of reported projectUuid:specName and clear capacity for each
            const porjectIdSpecNamePairs = (await client.sendCommand([
              "SMEMBERS",
              `zz_instance:${instanceIdN}`,
            ])) as `${string}:${string}`[];

            for (const pair of porjectIdSpecNamePairs) {
              const [projectUuidN, specNameN] = pair.split(":");
              // await client.sendCommand([
              //   "HDEL",
              //   `zz_maxcapacity:${projectUuidN}:${specNameN}`,
              //   instanceIdN,
              // ]);

              // remove capacity hash values
              await client.sendCommand([
                "HDEL",
                `zz_maxcapacity:${projectUuidN}:${specNameN}`,
                instanceIdN,
              ]);
              delete this.resolveByInstanceIAndSpecName[instanceIdN][specNameN];
              context.signal.removeEventListener("abort", abortListener);
            }
          };

          context.signal.addEventListener("abort", abortListener);
        } else {
          throw new Error("Invalid command");
        }
      }
    })();
    return iter;
  }

  async increaseCapacity({
    projectUuid,
    specName,
    by,
    conditionStillMet,
  }: {
    projectUuid: string;
    specName: string;
    by: number;
    conditionStillMet: () => Promise<boolean>;
  }) {
    // set the relevant capacity in redis
    const client = await this.redisClientP;
    const projectUuidN = escapeColon(projectUuid);
    const specNameN = escapeColon(specName);

    let instanceIdsAndMaxCapacities: string[] = [];

    // keep checking until we find an instance with capacity
    while (await conditionStillMet()) {
      // get all instances (keys) for this projectUuid:specName
      instanceIdsAndMaxCapacities = (await client.sendCommand([
        "HGETALL",
        `zz_maxcapacity:${projectUuidN}:${specNameN}`,
      ])) as string[];
      if (instanceIdsAndMaxCapacities.length === 0) {
        // console.warn(
        //   `No instances found for ${projectUuid}:${specName}. Will retry again in 2000ms.`
        // );
        await new Promise((r) => setTimeout(r, 2000));
        continue;
      } else {
        break;
      }
    }

    console.log(
      `instanceIdsAndMaxCapacities: for ${projectUuid}:${specName}: `,
      instanceIdsAndMaxCapacities
    );

    const maxCapacitiesByInstanceId = Object.fromEntries(
      _.chunk(instanceIdsAndMaxCapacities).map(
        ([instanceId, capacityStr]) =>
          [instanceId, Number(capacityStr)] as const
      )
    );

    console.log(
      `maxCapacitiesByInstanceId: for ${projectUuid}:${specName}: `,
      maxCapacitiesByInstanceId
    );

    // choose the instance with the most capacity
    const sorted = Object.entries(maxCapacitiesByInstanceId).sort(
      ([, a], [, b]) => b - a
    );

    let nextBestInstanceId: string | undefined;

    while (true) {
      nextBestInstanceId = sorted.shift()?.[0];
      if (!nextBestInstanceId) {
        console.warn(
          `No instances found for ${projectUuid}:${specName}. Exiting.`
        );
        break;
      }
      // nudge the instance to increase capacity
      const resolve =
        this.resolveByInstanceIAndSpecName[nextBestInstanceId]?.[specName];
      if (!resolve) {
        continue;
      }

      resolve({
        projectUuid,
        instanceId: nextBestInstanceId,
        provision: {
          projectUuid,
          specName,
          numberOfWorkersNeeded: by,
        },
      });
      break;
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
