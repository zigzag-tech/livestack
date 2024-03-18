import {
  AuthorizedGRPCClient,
  genAuthorizedVaultClient,
} from "@livestack/vault-client";
import { IStorageProvider } from "../storage/cloudStorage";
import { Stream } from "stream";
import chalk, { green, inverse, red, yellow } from "ansis";
import fs from "fs";

import { z } from "zod";
import { v4 } from "uuid";
import { sleep } from "../utils/sleep";
import limit from "p-limit";
const limiter = limit(1);
const LIVESTACK_DASHBOARD_URL_ROOT =
  process.env.LIVESTACK_DASHBOARD_URL_ROOT || "https://live.dev";
interface EnvParams {
  readonly storageProvider?: IStorageProvider;
  readonly projectId: string;
}

export class ZZEnv implements EnvParams {
  public readonly storageProvider?: IStorageProvider;
  private readonly _projectId: string;
  private static _vaultClientP: Promise<AuthorizedGRPCClient>;

  public static async vaultClient() {
    ZZEnv._ensureInitialized();
    return await ZZEnv._vaultClientP;
  }

  public static _ensureInitialized() {
    if (!ZZEnv._zzEnvP) {
      ZZEnv._zzEnvP = Promise.all([
        new Promise<ZZEnv>((resolve) => {
          ZZEnv._resolveGlobal = resolve;
        }),
      ]).then(([env]) => env);
      ZZEnv._vaultClientP = ZZEnv._zzEnvP.then((zzEnv) =>
        zzEnv
          .getAuthToken()
          .then((authToken) => genAuthorizedVaultClient(authToken))
      );
    }
  }
  private static _zzEnvP: Promise<ZZEnv> | null = null;
  private static _resolveGlobal: ((env: ZZEnv) => void) | null = null;

  static globalP() {
    ZZEnv._ensureInitialized();
    return ZZEnv._zzEnvP!;
  }

  static setGlobal(env: ZZEnv) {
    ZZEnv._ensureInitialized();
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

  private async printLiveDevUrlOnce() {
    const userId = await this.getAuthToken();
    if (!this.livePrinted) {
      console.info(
        yellow`${inverse` 🔴 LIVE 🦓🦓 https://live.dev/p/${userId}/${this._projectId}`}${inverse``}`
      );

      this.livePrinted = true;
    }
  }

  public static async getInstanceId() {
    ZZEnv._ensureInitialized();

    if (!this._cachedInstanceId) {
      const r = await(await ZZEnv.vaultClient()).queue.initInstance({});
      this._cachedInstanceId = r.instanceId;
    }
    return this._cachedInstanceId;
  }

  public getAuthToken = () => {
    return limiter(async () => {
      // read from file .livestack_auth
      // if it doesn't exist, request credentials by printing a URL
      const filename = ".livestack_auth";
      let userId: string | null = null;
      try {
        userId = fs.readFileSync(filename, "utf-8");
      } catch (e) {
        return "fake";
        // TODO: use more robust random string
        const cliTempToken = await getCliTempToken();
        // console.info(yellow`No local livestack dashboard credentials found.`);
        console.info(yellow`To contine, get a Livestack token here:`);
        console.info(
          green`>>> ${LIVESTACK_DASHBOARD_URL_ROOT}/start-cli?t=${cliTempToken} <<<`
        );
        console.info(yellow`(Or copy & paste the link in a browser)`);
        const { userToken, username } = await waitUntilCredentialsAreResolved(
          cliTempToken
        );
        fs.writeFileSync(filename, userToken, "utf-8");
        // print welcome message
        console.info(
          green`🦓 Welcome to Livestack, ${username}! Your token has been saved to ${filename}.`
        );
      }
      return userId!;
    });
  };

  public derive(newP: Partial<EnvParams>) {
    return new ZZEnv({
      ...this,
      ...newP,
    });
  }
}

async function getCliTempToken() {
  const randomTokenResp = await fetch(
    `${LIVESTACK_DASHBOARD_URL_ROOT}/cli-tokens`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
    }
  );
  const { cliTempToken } = await randomTokenResp.json();
  return cliTempToken;
}

async function waitUntilCredentialsAreResolved(cliTempToken: string) {
  // periodically make fetch request to see if the credentials have been fulfilled
  let secondsElapsed = 0;
  while (true) {
    const zebraLine = genZebraLine(secondsElapsed);
    // erase the previous line
    process.stdout.write("\r" + Array(zebraLine.length + 1).join(" "));
    // move cursor back to the start of the line
    process.stdout.write("\r");
    // print the new line with zebras
    process.stdout.write(zebraLine);
    const status = await getClITempTokenStatus(cliTempToken);
    if (status.status === "resolved") {
      return status;
    }

    await sleep(1000);
    secondsElapsed++;
  }
}

const LINE_LENGTH = 50; // Define the total length of the line

function genZebraLine(step: number) {
  let zebra1Pos = (LINE_LENGTH - step) % LINE_LENGTH; // Initial position for the first zebra
  let zebra2Pos = (zebra1Pos + Math.floor(LINE_LENGTH / 2)) % LINE_LENGTH; // Ensure the second zebra starts from the opposite side

  // Generate the line with zebras
  let line = Array.from({ length: LINE_LENGTH }, (_, i) =>
    i === zebra1Pos || i === zebra2Pos ? "🦓" : "-"
  ).join("");

  return line;
}
async function getClITempTokenStatus(cliTempToken: string): Promise<
  | {
      status: "waiting-to-resolve";
    }
  | {
      status: "resolved";
      userToken: string;
      username: string;
    }
> {
  const resp = await fetch(
    `${LIVESTACK_DASHBOARD_URL_ROOT}/cli-tokens/${cliTempToken}`,
    {
      method: "GET",
      headers: {
        "Content-Type": "application/json",
      },
    }
  );
  const s = await resp.json();
  return s;
}

export const fileOrBufferSchema = z.custom<Buffer | Stream>((data) => {
  return data instanceof Buffer || data instanceof Stream;
}, "Data is not an instance of a Buffer or a Stream.");
