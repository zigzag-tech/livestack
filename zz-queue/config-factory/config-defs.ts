import { injectable, inject } from "inversify";
import Redis, { RedisOptions } from "ioredis";

export interface ProjectConfig {
  projectId: string;
}
