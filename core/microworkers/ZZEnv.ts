import { Knex } from "knex";
import { IStorageProvider } from "../storage/cloudStorage";
import { RedisOptions } from "ioredis";
import { Stream } from "stream";
import { z } from "zod";

interface EnvParams {
  readonly storageProvider?: IStorageProvider;
  readonly projectId: string;
  readonly db: Knex;
  readonly redisConfig: RedisOptions;
}

export class ZZEnv implements EnvParams {
  public readonly storageProvider?: IStorageProvider;
  public readonly projectId: string;
  public readonly db: Knex;
  public readonly redisConfig: RedisOptions;
  private static _zzEnv: ZZEnv;

  static global() {
    if (!ZZEnv._zzEnv) throw new Error("ZZEnv has not been set");
    return ZZEnv._zzEnv;
  }

  static setGlobal(env: ZZEnv) {
    ZZEnv._zzEnv = env;
  }

  constructor({ storageProvider, projectId, db, redisConfig }: EnvParams) {
    this.storageProvider = storageProvider;
    this.projectId = projectId;
    this.db = db;
    this.redisConfig = redisConfig;
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
