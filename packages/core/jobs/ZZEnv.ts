import { IStorageProvider } from "../storage/cloudStorage";
import { Stream } from "stream";
import { z } from "zod";
import { RedisClientOptions } from "redis";
interface EnvParams {
  readonly storageProvider?: IStorageProvider;
  readonly projectId: string;
  readonly redisConfig?: RedisClientOptions;
}

export class ZZEnv implements EnvParams {
  public readonly storageProvider?: IStorageProvider;
  public readonly projectId: string;
  public readonly redisConfig: RedisClientOptions;
  private static _zzEnv: ZZEnv | null = null;

  static global() {
    return ZZEnv._zzEnv;
  }

  static setGlobal(env: ZZEnv) {
    ZZEnv._zzEnv = env;
  }

  constructor({
    storageProvider,
    projectId,
    redisConfig,
  }: Omit<EnvParams, "projectId"> & {
    projectId?: string;
  }) {
    this.storageProvider = storageProvider;
    if (!projectId) {
      projectId = "live-project-" + new Date().getTime();
      console.warn(
        "No projectId provided to ZZEnv. Giving it a default one: ",
        projectId
      );
      // fs.writeFileSync("PROJECT_ID", projectId);
    }
    this.projectId = projectId;
    this.redisConfig = redisConfig || {};

    // TODO: make better global resolution logic
    if (!ZZEnv.global()) {
      ZZEnv.setGlobal(this);
    }
  }

  public derive(newP: Partial<EnvParams>) {
    return new ZZEnv({
      ...this,
      ...newP,
    });
  }
}

export const fileOrBufferSchema = z.custom<Buffer | Stream>((data) => {
  return data instanceof Buffer || data instanceof Stream;
}, "Data is not an instance of a Buffer or a Stream.");
