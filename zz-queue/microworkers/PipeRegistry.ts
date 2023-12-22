import { z } from "zod";
import { IStorageProvider } from "../storage/cloudStorage";
import { Knex } from "knex";
import { RedisOptions } from "ioredis";

export class PipeDef<P, O, StreamI> {
  name: string;
  jobParams: z.ZodType<P>;
  output: z.ZodType<O>;
  streamInput: z.ZodType<StreamI>;
  constructor({
    name,
    jobParams,
    output,
    streamInput,
  }: {
    name: string;
    jobParams: z.ZodType<P>;
    output: z.ZodType<O>;
    streamInput: z.ZodType<StreamI>;
  }) {
    this.name = name;
    this.jobParams = jobParams;
    this.output = output;
    this.streamInput = streamInput;
  }
}

export class ZZEnv {
  public readonly storageProvider?: IStorageProvider;
  public readonly projectId: string;
  public readonly db: Knex;
  public readonly redisConfig: RedisOptions;

  constructor({
    storageProvider,
    projectId,
    db,
    redisConfig,
  }: {
    storageProvider?: IStorageProvider;
    projectId: string;
    db: Knex;
    redisConfig: RedisOptions;
  }) {
    this.storageProvider = storageProvider;
    this.projectId = projectId;
    this.db = db;
    this.redisConfig = redisConfig;
  }
}
