import {
  AuthorizedGRPCClient,
  genAuthorizedVaultClient,
} from "@livestack/vault-client";
import { IStorageProvider } from "../storage/cloudStorage";
import { Stream } from "stream";
import chalk, { blueBright, green, inverse, red, yellow } from "ansis";
import fs from "fs";
import { z } from "zod";
import { sleep } from "../utils/sleep";
import limit from "p-limit";
import {
  ResolvedCliTokenStatusWithUserToken,
  WaitingTorResolveCliTokenStatus,
} from "../onboarding/CliOnboarding";
import { parse, stringify } from "@ltd/j-toml";
import path from "path";
import os from "os";


const limiter = limit(1);
export const LIVESTACK_DASHBOARD_URL_ROOT =
  process.env.LIVESTACK_DASHBOARD_URL_ROOT || "https://live.dev";
interface ZZEnvConfig {
  readonly storageProvider?: IStorageProvider;
  // two types of project IDs are supported:
  // 1. "@<userId>/<projectUuid>"
  // 2. "<projectUuid>", in which case the userId is assumed to be the current user's
  readonly projectId: string | `@${string}/${string}`;
}

export class ZZEnv {
  public readonly storageProvider?: IStorageProvider;
  public readonly vaultClient: AuthorizedGRPCClient;
  public readonly projectUuid: string;
  public readonly localProjectId: string;
  public readonly userId: string;
  public readonly authToken: string;

  private constructor({
    storageProvider,
    projectUuid,
    localProjectId,
    userId,
    authToken,
  }: {
    storageProvider?: IStorageProvider;
    projectUuid: string;
    localProjectId: string;
    userId: string;
    authToken: string;
  }) {
    this.storageProvider = storageProvider;
    this.projectUuid = projectUuid;
    this.localProjectId = localProjectId;
    this.userId = userId;
    this.authToken = authToken;
    this.vaultClient = genAuthorizedVaultClient(authToken);
    this.printLiveDevUrlOnce();
  }

  static async create(config: ZZEnvConfig) {
    const { projectUuid, localProjectId, userId, authToken } =
      await resolveProjectInfo(config.projectId);

    return new ZZEnv({
      storageProvider: config.storageProvider,
      projectUuid,
      localProjectId,
      userId,
      authToken,
    });
  }

  private static _zzEnvP: Promise<ZZEnv> | null = null;
  private static _resolveGlobal: ((env: ZZEnv) => void) | null = null;

  public static _ensureInitialized() {
    if (!ZZEnv._zzEnvP) {
      ZZEnv._zzEnvP = new Promise<ZZEnv>((resolve) => {
        ZZEnv._resolveGlobal = resolve;
      });
    }
  }

  static globalP() {
    ZZEnv._ensureInitialized();
    return ZZEnv._zzEnvP!;
  }

  static setGlobal(env: ZZEnv | Promise<ZZEnv>) {
    ZZEnv._ensureInitialized();
    if (env instanceof ZZEnv) {
      console.info("Global project ID set to ", env.projectUuid);
      ZZEnv._resolveGlobal!(env);
    } else {
      env.then((env) => {
        console.info("Global project ID set to ", env.projectUuid);
        ZZEnv._resolveGlobal!(env);
      });
    }
  }

  private _cachedInstanceId: string | null = null;
  private livePrinted = false;

  private async printLiveDevUrlOnce() {
    if (!this.livePrinted) {
      console.info(
        yellow`${inverse` 🔴 LIVE 🦓🦓 ${LIVESTACK_DASHBOARD_URL_ROOT}/p/${this.userId}/${this.localProjectId}`}${inverse``}`
      );

      this.livePrinted = true;
    }
  }

  static getInstanceLimiter = limit(1);

  public async getInstanceId() {
    return ZZEnv.getInstanceLimiter(async () => {
      if (!this._cachedInstanceId) {
        const r = await this.vaultClient.queue.initInstance({});
        this._cachedInstanceId = r.instanceId;
        console.info("Instance ID set to ", this._cachedInstanceId);
      }
      return this._cachedInstanceId;
    });
  }
}

async function resolveProjectInfo(projectId: string) {
  let userId: string;
  let localProjectId: string;
  let authToken: string;

  if (projectId.startsWith("@") && projectId.includes("/")) {
    [userId, localProjectId] = projectId.slice(1).split("/") as [
      `@${string}`,
      `${string}`
    ];
    userId = userId.slice(1);
    ({ authToken } = await getOrPromptForUserCredentials(localProjectId));
  } else {
    localProjectId = projectId;
    ({ userId, authToken } = await getOrPromptForUserCredentials(
      localProjectId
    ));
  }

  const resp = await fetch(
    `${LIVESTACK_DASHBOARD_URL_ROOT}/api/v1/projects?userId=${userId}&localProjectId=${localProjectId}`,
    {
      method: "GET",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${authToken}`,
      },
    }
  );

  if (resp.status === 404) {
    throw new Error(
      `Project ${localProjectId} under user ${userId} not found.`
    );
  } else if (!resp.ok) {
    throw new Error("Failed to fetch project.");
  } else {
    const [{ projectUuid }] = (await resp.json()) as [{ projectUuid: string }];
    return { projectUuid, userId, localProjectId, authToken };
  }
}

const getOrPromptForUserCredentials: (localProjectId: string) => Promise<{
  authToken: string;
  userId: string;
}> = (localProjectId: string) => {
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
        const cliTempToken = await getCliTempToken(localProjectId);
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
          userDisplayName,
          userId,
        } = await waitUntilCredentialsAreResolved(cliTempToken);

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

async function getCliTempToken(localProjectId: string) {
  const randomTokenResp = await fetch(
    `${LIVESTACK_DASHBOARD_URL_ROOT}/api/v1/cli-tokens`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        localProjectId,
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
