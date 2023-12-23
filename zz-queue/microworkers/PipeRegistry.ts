import { z } from "zod";
import { IStorageProvider } from "../storage/cloudStorage";
import { Knex } from "knex";
import { RedisOptions } from "ioredis";
interface PipeParams<P, O, WP, StreamI, TProgress> {
  name: string;
  jobParams: z.ZodType<P>;
  workerInstanceParams?: z.ZodType<WP>;
  output: z.ZodType<O>;
  streamInput?: z.ZodType<StreamI>;
  progress?: z.ZodType<TProgress>;
}
export class PipeDef<
  P,
  O,
  WP extends object = never,
  StreamI = never,
  TProgress = never
> implements PipeParams<P, O, WP, StreamI, TProgress>
{
  name: string;
  jobParams: z.ZodType<P>;
  workerInstanceParams?: z.ZodType<WP>;
  output: z.ZodType<O>;
  streamInput: z.ZodType<StreamI>;
  progress: z.ZodType<TProgress>;

  constructor({
    name,
    jobParams,
    output,
    streamInput,
    progress,
    workerInstanceParams,
  }: PipeParams<P, O, WP, StreamI, TProgress>) {
    this.name = name;
    this.jobParams = jobParams;
    this.output = output;
    this.streamInput = streamInput || z.never();
    this.progress = progress || z.never();
    this.workerInstanceParams = workerInstanceParams;
  }

  public derive<NewP, NewO, newWP, NewStreamI, NewTP>(
    newP: Partial<PipeParams<NewP, NewO, newWP, NewStreamI, NewTP>>
  ) {
    return new PipeDef({
      ...this,
      ...newP,
    } as PipeParams<P & (NewP | {}), O & (NewO | {}), WP & (newWP | {}), StreamI & (NewStreamI | {}), TProgress & (NewTP | {})>);
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
