import type { ZodType } from "zod";
import { saveLargeFilesToStorage } from "../storage/cloudStorage";
import { LiveEnv, LIVESTACK_DASHBOARD_URL_ROOT } from "../jobs/LiveEnv";
import path from "path";
import { zodToJsonSchema } from "zod-to-json-schema";
import {
  identifyLargeFilesToRestore,
  identifyLargeFilesToSave,
  restoreLargeValues,
} from "../files/file-ops";
import { getLogger } from "../utils/createWorkerLogger";
import { StreamDatapoint } from "@livestack/vault-interface/src/generated/stream";
import { Empty } from "@livestack/vault-interface";

export class DataStream<T extends object> {
  public readonly def: ZodType<T> | null;
  public readonly uniqueName: string;
  public readonly liveEnvP: Promise<LiveEnv>;
  baseWorkingRelativePathP: Promise<string>;

  private logger: ReturnType<typeof getLogger>;
  protected static globalRegistry: { [key: string]: DataStream<any> } = {};
  public static async getOrCreate<T extends object>({
    uniqueName,
    def,
    liveEnv,
    logger,
  }: {
    uniqueName: string;
    def?: ZodType<T> | null;
    liveEnv?: LiveEnv | null;
    logger?: ReturnType<typeof getLogger>;
  }): Promise<DataStream<T>> {
    if (liveEnv) {
      liveEnv = await LiveEnv.globalP();
    }
    if (DataStream.globalRegistry[uniqueName]) {
      const existing = DataStream.globalRegistry[uniqueName];
      // check if types match
      // TODO: use a more robust way to check if types match
      // TODO: to bring back this check
      // if (def) {
      //   if (existing.hash !== hashDef(def)) {
      //     throw new Error(
      //       `DataStream ${uniqueName} already exists with different type, and the new type provided is not compatible with the existing type.`
      //     );
      //   }
      // }
      return existing as DataStream<T>;
    } else {
      if (!logger) {
        throw new Error(
          "def and logger must be provided if stream does not exist."
        );
      }
      const stream = new DataStream({
        uniqueName,
        def: def || null,
        liveEnv,
        logger,
      });
      // async
      if (liveEnv) {
        let jsonSchemaStr: string | undefined = undefined;
        if (def) {
          const jsonSchema = zodToJsonSchema(def);
          jsonSchemaStr = JSON.stringify(jsonSchema);
        }
        await (
          await LiveEnv.globalP()
        ).vaultClient.stream.ensureStream({
          project_uuid: liveEnv.projectUuid,
          stream_id: uniqueName,
          json_schema_str: jsonSchemaStr,
        });
      }
      DataStream.globalRegistry[uniqueName] = stream;
      return stream;
    }
  }

  protected constructor({
    uniqueName,
    def,
    liveEnv,
    logger,
  }: {
    uniqueName: string;
    def: ZodType<T> | null;
    liveEnv?: LiveEnv | null;
    logger: ReturnType<typeof getLogger>;
  }) {
    this.def = def;
    this.uniqueName = uniqueName;
    if (liveEnv) {
      this.liveEnvP = Promise.resolve(liveEnv);
    } else {
      this.liveEnvP = LiveEnv.globalP();
    }

    this.logger = logger;
    this.baseWorkingRelativePathP = this.liveEnvP.then((liveEnv) =>
      path.join(liveEnv.projectUuid, this.uniqueName)
    );
  }

  private async processDatapoint(resp: {
    datapoint?: StreamDatapoint;
    null_response?: Empty;
  }) {
    const { datapoint, null_response } = resp;
    if (null_response) {
      return null;
    } else if (datapoint) {
      const data = JSON.parse(datapoint.dataStr);

      let restored = data;
      const { largeFilesToRestore, newObj } = identifyLargeFilesToRestore(data);

      if (largeFilesToRestore.length > 0) {
        const liveEnv = await this.liveEnvP;
        const storageProvider = await liveEnv.storageProvider;
        if (!storageProvider) {
          throw new Error(
            "storageProvider is not provided, and not all parts can be saved to local storage because they are either too large or contains binary data."
          );
        } else {
          restored = (await restoreLargeValues({
            obj_: newObj,
            largeFilesToRestore,
            basePath: await this.baseWorkingRelativePathP,
            fetcher: storageProvider.fetchFromStorage,
          })) as T;
        }
      }
      return {
        ...restored,
        timestamp: datapoint.timestamp,
        chunkId: datapoint.chunkId,
      } as WithTimestamp<T>;
    } else {
      throw new Error("Unexpected response from lastValue");
    }
  }

  public allValues = async () => {
    const { datapoints } = await (
      await (
        await LiveEnv.globalP()
      ).vaultClient
    ).stream.allValues({
      projectUuid: (await this.liveEnvP).projectUuid,
      uniqueName: this.uniqueName,
    });

    const wLargeValues = await Promise.all(
      datapoints.map(this.processDatapoint.bind(this))
    );

    return wLargeValues;
  };

  public valuesByReverseIndex = async (lastN: number) => {
    const { datapoints } = await (
      await (
        await LiveEnv.globalP()
      ).vaultClient
    ).stream.valuesByReverseIndex({
      projectUuid: (await this.liveEnvP).projectUuid,
      uniqueName: this.uniqueName,
      lastN,
    });

    const wLargeValues = await Promise.all(
      datapoints.map(this.processDatapoint.bind(this))
    );

    return wLargeValues;
  };

  public async pub({
    message,
    jobInfo,
    parentDatapoints,
  }: {
    message: T;
    jobInfo?: {
      jobId: string;
      outputTag: string;
    };
    parentDatapoints: {
      streamId: string;
      datapointId: string;
    }[];
  }) {
    let parsed: T;
    parsed = message;
    // if (!this.def) {
    //   parsed = message;
    // } else {
    //   try {
    //     parsed = this.def.parse(message) as T;
    //   } catch (err) {
    //     console.error(
    //       this.uniqueName,
    //       " errornous output: ",
    //       JSON.stringify(message, null, 2)
    //     );
    //     this.logger.error(
    //       "Expected type: " + JSON.stringify(zodToJsonSchema(this.def), null, 2)
    //     );
    //     this.logger.error(
    //       "Error while validating data points on stream " +
    //         this.uniqueName +
    //         ": " +
    //         JSON.stringify(err)
    //     );
    //     throw err;
    //   }
    // }

    let { largeFilesToSave, newObj } = await identifyLargeFilesToSave(parsed);
    const liveEnv = await this.liveEnvP;
    const storageProvider = await liveEnv.storageProvider;
    if (storageProvider) {
      const basePath = await this.baseWorkingRelativePathP;
      const fullPathLargeFilesToSave = largeFilesToSave.map((x) => ({
        ...x,
        path: path.join(basePath, x.path),
      }));

      if (fullPathLargeFilesToSave.length > 0) {
        // this.logger.info(
        //   `Saving large files to storage: ${fullPathLargeFilesToSave
        //     .map((x) => x.path)
        //     .join(", ")}`
        // );
        await saveLargeFilesToStorage(
          fullPathLargeFilesToSave,
          storageProvider
        );
        parsed = newObj;
      }
    } else {
      if (largeFilesToSave.length > 0) {
        throw new Error(
          "storageProvider is not provided, and not all parts can be saved to local storage because they are either too large or contains binary data."
        );
      }
    }

    try {
      // Publish the data to the stream
      // console.debug(
      //   "Data point added to stream",
      //   this.uniqueName,
      //   JSON.stringify(parsed)
      // );
      const vaultClient = await(await LiveEnv.globalP()).vaultClient;
      const { success, validationFailure } = await vaultClient.stream.pub({
        streamId: this.uniqueName,
        projectUuid: (await this.liveEnvP).projectUuid,
        jobInfo: jobInfo,
        dataStr: JSON.stringify(parsed),
        parentDatapoints,
      });

      if (validationFailure) {
        console.error(
          `Error publishing to stream ${this.uniqueName}:`,
          validationFailure,
          "; Data being sent: ",
          JSON.stringify((parsed as any).data || parsed, null, 2).slice(0, 500)
        );
        const { datapointId, errorMessage } = validationFailure;

        const { userId } = await await this.liveEnvP;
        const inspectMessage = ` 🔍🔴 Inspect error:  ${LIVESTACK_DASHBOARD_URL_ROOT}/p/${userId}/${
          (await this.liveEnvP).localProjectId
        }`;
        console.info(inspectMessage);
        console.error("Error message: ", errorMessage);

        throw new Error(`Validation failed. ${inspectMessage}`);
      } else if (success) {
        const { chunkId, datapointId } = success;
        return chunkId;
      }
    } catch (error) {
      console.error(
        `Error publishing to stream ${this.uniqueName}:`,
        error,
        "data: ",
        JSON.stringify(parsed, null, 2)
      );

      throw error;
    }
  }

  public static define<T extends object>(
    ...p: ConstructorParameters<typeof DataStreamDef<T>>
  ) {
    return new DataStreamDef<T>(...p);
  }
}

export class DataStreamDef<T> {
  public readonly streamDefName: string;
  public readonly def?: ZodType<T>;

  constructor(streamDefName: string, def?: ZodType<T>) {
    this.streamDefName = streamDefName;
    this.def = def;
  }
}

export type WithTimestamp<T extends object> = T & {
  timestamp: number;
  chunkId: string;
  datapointId: string;
};
