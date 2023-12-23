import { z } from "zod";
import { IStorageProvider } from "../storage/cloudStorage";
import { Knex } from "knex";
import { RedisOptions } from "ioredis";
interface PipeParams<P, O, StreamI, S> {
  name: string;
  jobParams: z.ZodType<P>;
  output: z.ZodType<O>;
  streamInput?: z.ZodType<StreamI>;
  status?: z.ZodType<S>;
}
export class PipeDef<P, O, StreamI = never, S = never>
  implements PipeParams<P, O, StreamI, S>
{
  name: string;
  jobParams: z.ZodType<P>;
  output: z.ZodType<O>;
  streamInput: z.ZodType<StreamI>;
  status: z.ZodType<S>;

  constructor({
    name,
    jobParams,
    output,
    streamInput,
    status,
  }: PipeParams<P, O, StreamI, S>) {
    this.name = name;
    this.jobParams = jobParams;
    this.output = output;
    this.streamInput = streamInput || z.never();
    this.status = status || z.never();
  }

  public derive<NewP, NewO, NewStreamI, newS>(
    newP: Partial<PipeParams<NewP, NewO, NewStreamI, newS>>
  ) {
    return new PipeDef({
      ...this,
      ...newP,
    });
  }
}

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
