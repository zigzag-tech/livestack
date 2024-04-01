import {
  AuthorizedGRPCClient,
  genAuthorizedVaultClient,
} from "@livestack/vault-client";
import { IStorageProvider } from "../storage/cloudStorage";
import { Stream } from "stream";
import chalk, { blueBright, green, inverse, red, yellow } from "ansis";
import fs from "fs";
import { z } from "zod";
import { v4 } from "uuid";
import { sleep } from "../utils/sleep";
import limit from "p-limit";
import {
  CLITempTokenStatus,
  ResolvedCliTokenStatusWithUserToken,
  WaitingTorResolveCliTokenStatus,
} from "../onboarding/CliOnboarding";
import { parse, stringify } from "@ltd/j-toml";
import path from "path";
import os from "os";

const limiter = limit(1);
export const LIVESTACK_DASHBOARD_URL_ROOT =
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

  public async vaultClient() {
    return await ZZEnv.vaultClient();
  }

  public static _ensureInitialized() {
    if (!ZZEnv._zzEnvP) {
      ZZEnv._zzEnvP = Promise.all([
        new Promise<ZZEnv>((resolve) => {
          ZZEnv._resolveGlobal = resolve;
        }),
      ]).then(([env]) => env);
      ZZEnv._vaultClientP = ZZEnv._zzEnvP.then((zzEnv) =>
        zzEnv.getUserCredentials().then(({ authToken }) => {
          return genAuthorizedVaultClient(authToken);
        })
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
    const { userId } = await this.getUserCredentials();
    if (!this.livePrinted) {
      console.info(
        yellow`${inverse` 🔴 LIVE 🦓🦓 ${LIVESTACK_DASHBOARD_URL_ROOT}/p/${userId}/${this._projectId}`}${inverse``}`
      );

      this.livePrinted = true;
    }
  }

  static getInstanceLimiter = limit(1);

  public static async getInstanceId() {
    return ZZEnv.getInstanceLimiter(async () => {
      ZZEnv._ensureInitialized();

      if (!this._cachedInstanceId) {
        const r = await (await ZZEnv.vaultClient()).queue.initInstance({});
        this._cachedInstanceId = r.instanceId;
        console.info("Instance ID set to ", this._cachedInstanceId);
      }
      return this._cachedInstanceId;
    });
  }

  public async getInstanceId() {
    return ZZEnv.getInstanceId();
  }

  public getUserCredentials: () => Promise<{
    authToken: string;
    userId: string;
  }> = () => {
    return limiter(async () => {
      const configDir = path.join(os.homedir(), ".livestack");
      const configFile = path.join(configDir, "config.toml");

      let config: any = {};
      try {
        const configData = fs.readFileSync(configFile, "utf-8");
        config = parse(configData);
      } catch (e) {
        // Config file doesn't exist, create it later
      }

      const hostRoot = LIVESTACK_DASHBOARD_URL_ROOT;
      // let authToken: string | null = null;

      if (config[hostRoot] && config[hostRoot].auth_token) {
        return {
          authToken: config[hostRoot].auth_token,
          userId: config[hostRoot].user_id,
        };
      } else {
        try {
          const cliTempToken = await getCliTempToken(this);
          const inBoxStr = ` ${LIVESTACK_DASHBOARD_URL_ROOT}/cli?t=${cliTempToken} `;
          const boxWidth = inBoxStr.length;
          const boxBorder = "╔" + "═".repeat(boxWidth) + "╗";
          const boxSides = "║";
          const boxBottom = "╚" + "═".repeat(boxWidth) + "╝";
          const toContinueMsg = `To continue, get your Livestack token here:`;
          console.info(blueBright(boxBorder));
          console.info(
            blueBright`${boxSides}${Array(
              Math.floor((inBoxStr.length + 2 - toContinueMsg.length) / 2)
            ).join(" ")}${toContinueMsg}${Array(
              Math.ceil((inBoxStr.length + 2 - toContinueMsg.length) / 2)
            ).join(" ")}${boxSides}`
          );
          console.info(
            blueBright`${boxSides}${Array(inBoxStr.length + 1).join(
              " "
            )}${boxSides}`
          );
          console.info(blueBright`${boxSides}${inBoxStr}${boxSides}`);
          console.info(
            blueBright`${boxSides}${Array(inBoxStr.length + 1).join(
              " "
            )}${boxSides}`
          );
          console.info(blueBright(boxBottom));
          console.info(yellow`(Or copy & paste the link in a browser)`);

          const {
            userToken: authToken,
            projectId,
            userDisplayName,
            userId,
          } = await waitUntilCredentialsAreResolved(cliTempToken);

          if (projectId !== this.projectId) {
            throw new Error("Project ID mismatch");
          }

          // Create the config directory if it doesn't exist
          if (!fs.existsSync(configDir)) {
            fs.mkdirSync(configDir);
          }

          // Update the config object with the new auth token
          config[hostRoot] = {
            auth_token: authToken,
            user_id: userId,
          };
          // Write the updated config to the file
          let configStrOrArr = stringify(config);
          let configStr: string;
          if (Array.isArray(configStrOrArr)) {
            configStr = configStrOrArr.join("\n");
          } else {
            configStr = configStrOrArr;
          }

          fs.writeFileSync(configFile, configStr, "utf-8");

          // Print welcome message
          // empty line
          console.info("");
          console.info(
            green`🦓 Welcome to Livestack${
              userDisplayName ? `, ${userDisplayName}` : ""
            }! Your token has been saved to ${configFile}.`
          );

          return {
            authToken,
            userId,
          };

          // console.info("Press any key to continue...");

          // // Wait for key press
          // process.stdin.setRawMode(true);
          // process.stdin.resume();
          // process.stdin.setEncoding("utf8");
          // await new Promise<void>((resolve) =>
          //   process.stdin.once("data", () => {
          //     resolve();
          //   })
          // );
          // process.stdin.setRawMode(false);
          // process.stdin.pause();
        } catch (e) {
          console.error(e);
          console.error(
            red`Failed to communicate with Livestack Cloud server. Please contact ZigZag support.`
          );
          throw e;
        }
      }
    });
  };

  public derive(newP: Partial<EnvParams>) {
    return new ZZEnv({
      ...this,
      ...newP,
    });
  }
}

async function getCliTempToken(zzEnvP: Promise<ZZEnv> | ZZEnv) {
  const randomTokenResp = await fetch(
    `${LIVESTACK_DASHBOARD_URL_ROOT}/api/v1/cli-tokens`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        projectId: (await zzEnvP).projectId,
      }),
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
    i === zebra1Pos || i === zebra2Pos ? "🦓" : "―"
  ).join("");

  return line;
}
async function getClITempTokenStatus(
  cliTempToken: string
): Promise<
  WaitingTorResolveCliTokenStatus | ResolvedCliTokenStatusWithUserToken
> {
  const resp = await fetch(
    `${LIVESTACK_DASHBOARD_URL_ROOT}/api/v1/cli-tokens/${cliTempToken}`,
    {
      method: "GET",
      headers: {
        "Content-Type": "application/json",
      },
    }
  );
  const s = (await resp.json()) as
    | WaitingTorResolveCliTokenStatus
    | ResolvedCliTokenStatusWithUserToken;
  return s;
}

export const fileOrBufferSchema = z.custom<Buffer | Stream>((data) => {
  return data instanceof Buffer || data instanceof Stream;
}, "Data is not an instance of a Buffer or a Stream.");
