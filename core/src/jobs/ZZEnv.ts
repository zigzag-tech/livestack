import { vaultClient } from "@livestack/vault-client";
import { IStorageProvider } from "../storage/cloudStorage";
import { Stream } from "stream";
import chalk, { green, inverse, red, yellow } from "ansis";
import fs from "fs";

import { z } from "zod";
interface EnvParams {
  readonly storageProvider?: IStorageProvider;
  readonly projectId: string;
}

export class ZZEnv implements EnvParams {
  public readonly storageProvider?: IStorageProvider;
  private readonly _projectId: string;

  public static _initialize() {
    if (!ZZEnv._zzEnvP) {
      ZZEnv._zzEnvP = new Promise((resolve) => {
        ZZEnv._resolveGlobal = resolve;
      });
    }
  }
  private static _zzEnvP: Promise<ZZEnv> | null = null;
  private static _resolveGlobal: ((env: ZZEnv) => void) | null = null;

  static globalP() {
    ZZEnv._initialize();
    return ZZEnv._zzEnvP!;
  }

  static setGlobal(env: ZZEnv) {
    ZZEnv._initialize();
    console.info("Global project ID set to ", env._projectId);
    ZZEnv._resolveGlobal!(env);
  }

  constructor({
    storageProvider,
    projectId,
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

    this._projectId = projectId;
  }

  get projectId() {
    this.printLiveDevUrlOnce();
    return this._projectId;
  }

  private static _cachedInstanceId: string | null = null;
  private livePrinted = false;

  private printLiveDevUrlOnce() {
    if (!this.livePrinted) {
      console.info(
        yellow`${inverse` 🔴 LIVE 🦓🦓 https://live.dev/p/test-user/${this._projectId}`}${inverse``}`
      );

      this.livePrinted = true;
    }
  }

  public static async getInstanceId() {
    ZZEnv._initialize();
    if (!this._cachedInstanceId) {
      const r = await vaultClient.queue.initInstance({});
      this._cachedInstanceId = r.instanceId;
    }
    return this._cachedInstanceId;
  }

  public async getUserId() {
    // read from file .livestack_user_id
    // if it doesn't exist, create it with a random uuid
    // return the uuid
    const filename = ".livestack_user_id";
    let userId: string | null = null;
    try {
      userId = fs.readFileSync(filename, "utf-8");
    } catch (e) {
      userId = require("uuid").v4();
      fs.writeFileSync(filename, userId!);
    }
    return userId;
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
