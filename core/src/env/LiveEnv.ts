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
} from "../onboarding";
import { parse, stringify } from "@ltd/j-toml";
import path from "path";
import os from "os";

const CONN_FAILED_MSG = "Please make sure you are connected to the internet.";

const limiter = limit(1);
export const LIVESTACK_DASHBOARD_URL_ROOT =
  process.env.LIVESTACK_DASHBOARD_URL_ROOT || "https://live.dev";
interface LiveEnvConfig {
  readonly storageProvider?: Promise<IStorageProvider> | IStorageProvider;
  // two types of project IDs are supported:
  // 1. "@<userId>/<projectUuid>"
  // 2. "<projectUuid>", in which case the userId is assumed to be the current user's
  readonly projectId?: string | `@${string}/${string}`;
}

/**
 * Represents the live environment for managing storage and vault client.
 */
export class LiveEnv {
  /**
   * Storage provider for handling large data storage.
   */
  public readonly storageProvider?: Promise<IStorageProvider | undefined>;
  /**
   * Vault client for managing secure data.
   */
  public readonly vaultClient: AuthorizedGRPCClient;
  /**
   * UUID of the project.
   */
  public readonly projectUuid: string;
  /**
   * Local project ID.
   */
  public readonly localProjectId: string;
  /**
   * User ID.
   */
  public readonly userId: string;
  /**
   * Authentication token.
   */
  public readonly authToken: string;

  /**
   * Constructs a new LiveEnv instance.
   *
   * @param storageProvider - The storage provider for handling large data storage.
   * @param projectUuid - The UUID of the project.
   * @param localProjectId - The local project ID.
   * @param userId - The user ID.
   * @param authToken - The authentication token.
   */
  private constructor({
    storageProvider,
    projectUuid,
    localProjectId,
    userId,
    authToken,
  }: {
    storageProvider?: IStorageProvider | Promise<IStorageProvider>;
    projectUuid: string;
    localProjectId: string;
    userId: string;
    authToken: string;
  }) {
    this.storageProvider = Promise.resolve(storageProvider);
    this.projectUuid = projectUuid;
    this.localProjectId = localProjectId;
    this.userId = userId;
    this.authToken = authToken;
    this.vaultClient = genAuthorizedVaultClient(authToken);
    this.printLiveDevUrlOnce();
  }

  /**
   * Creates a new LiveEnv instance.
   *
   * @param config - The configuration for the LiveEnv instance.
   * @returns A promise resolving to the new LiveEnv instance.
   */
  static async create(config: LiveEnvConfig): Promise<LiveEnv> {
    let projectId = config.projectId;
    if (!projectId) {
      projectId = process.env.LIVESTACK_PROJECT_ID;
    }

    if (!projectId) {
      throw new Error(
        "Project ID is not provided. Please provide a project ID with LiveEnv.create({ projectId: '...' }) or set the LIVESTACK_PROJECT_ID environment variable."
      );
    }
    const { projectUuid, localProjectId, userId, authToken } =
      await resolveProjectInfo(projectId);

    return new LiveEnv({
      storageProvider: config.storageProvider,
      projectUuid,
      localProjectId,
      userId,
      authToken,
    });
  }

  /**
   * Promise resolving to the global LiveEnv instance.
   */
  private static _liveEnvP: Promise<LiveEnv> | null = null;
  /**
   * Function to resolve the global LiveEnv instance.
   */
  private static _resolveGlobal: ((env: LiveEnv) => void) | null = null;

  /**
   * Ensures that the global LiveEnv instance is initialized.
   * @hidden
   */
  public static _ensureInitialized(): void {
    if (!LiveEnv._liveEnvP) {
      LiveEnv._liveEnvP = new Promise<LiveEnv>((resolve) => {
        LiveEnv._resolveGlobal = resolve;
      });
    }
  }

  /**
   * Gets the promise resolving to the global LiveEnv instance.
   *
   * @returns The promise resolving to the global LiveEnv instance.
   */
  static globalP(): Promise<LiveEnv> {
    LiveEnv._ensureInitialized();
    return LiveEnv._liveEnvP!;
  }

  /**
   * Sets the global LiveEnv instance.
   *
   * @param env - The LiveEnv instance or a promise resolving to it.
   */
  static setGlobal(env: LiveEnv | Promise<LiveEnv>): void {
    LiveEnv._ensureInitialized();
    if (env instanceof LiveEnv) {
      console.info("Global project ID set to ", env.projectUuid);
      LiveEnv._resolveGlobal!(env);
    } else {
      env.then((env) => {
        console.info("Global project ID set to ", env.projectUuid);
        LiveEnv._resolveGlobal!(env);
      });
    }
  }

  /**
   * Cached instance ID.
   */
  private _cachedInstanceId: string | null = null;
  /**
   * Flag indicating whether the live URL has been printed.
   */
  private livePrinted = false;

  /**
   * Prints the live development URL once.
   */
  private async printLiveDevUrlOnce(): Promise<void> {
    if (!this.livePrinted) {
      console.info(
        yellow`${inverse` 🔴 LIVE 🦓🦓 ${LIVESTACK_DASHBOARD_URL_ROOT}/p/${this.userId}/${this.localProjectId}`}${inverse``}`
      );

      this.livePrinted = true;
    }
  }

  /**
   * Limiter for ensuring a single instance.
   */
  static getInstanceLimiter = limit(1);

  /**
   * Gets the instance ID.
   *
   * @returns The instance ID.
   */
  public async getInstanceId(): Promise<string> {
    return LiveEnv.getInstanceLimiter(async () => {
      if (!this._cachedInstanceId) {
        const r = await this.vaultClient.queue.initInstance({});
        this._cachedInstanceId = r.instanceId;
        console.info("Instance ID set to ", this._cachedInstanceId);
      }
      return this._cachedInstanceId;
    });
  }
}

/**
 * Resolves project information based on the project ID.
 *
 * @param projectId - The project ID.
 * @returns The resolved project information.
 */
async function resolveProjectInfo(projectId: string): Promise<{
  projectUuid: string;
  userId: string;
  localProjectId: string;
  authToken: string;
}> {
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

  try {
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
      throw new Error(
        `Failed to fetch project from ${LIVESTACK_DASHBOARD_URL_ROOT}. Response status: ${resp.status}.`
      );
    } else {
      const [{ projectUuid }] = (await resp.json()) as [
        { projectUuid: string }
      ];
      return { projectUuid, userId, localProjectId, authToken };
    }
  } catch (e) {
    console.error(e);
    const errMsg =
      `Failed to fetch project from ${LIVESTACK_DASHBOARD_URL_ROOT}. ` +
      CONN_FAILED_MSG;
    throw new Error(errMsg);
  }
}

/**
 * Gets or prompts for user credentials.
 *
 * @param localProjectId - The local project ID.
 * @returns The user credentials.
 */
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
        const inBoxStr = `${LIVESTACK_DASHBOARD_URL_ROOT}/cli?t=${cliTempToken} `;

        const multiLineText = `\nTo continue, first get your Livestack token here:\n\n${inBoxStr}\n`;
        addDoubleBox(multiLineText);

        console.info(
          yellow`(If you cannot open the link, copy & paste it in a browser.)`
        );

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

function addDoubleBox(text: string) {
  const lines = text.split("\n");
  const maxLength = Math.max(...lines.map((line) => line.length));
  const boxWidth = maxLength + 2;
  const boxBorder = "╔" + "═".repeat(boxWidth) + "╗";
  const boxSides = "║";
  const boxBottom = "╚" + "═".repeat(boxWidth) + "╝";

  console.info(blueBright(boxBorder));
  for (const line of lines) {
    const padding = " ".repeat(maxLength - line.length);
    console.info(blueBright`${boxSides} ${line}${padding} ${boxSides}`);
  }
  console.info(blueBright(boxBottom));
}

/**
 * Gets a temporary CLI token.
 *
 * @param localProjectId - The local project ID.
 * @returns The temporary CLI token.
 */
async function getCliTempToken(localProjectId: string): Promise<string> {
  try {
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
  } catch (e) {
    console.error(e);
    throw new Error(
      `Failed to contact ${LIVESTACK_DASHBOARD_URL_ROOT}.` + CONN_FAILED_MSG
    );
  }
}

/**
 * Waits until the credentials are resolved.
 *
 * @param cliTempToken - The temporary CLI token.
 * @returns The resolved credentials.
 */
async function waitUntilCredentialsAreResolved(
  cliTempToken: string
): Promise<ResolvedCliTokenStatusWithUserToken> {
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

/**
 * Generates a zebra line for CLI output.
 *
 * @param step - The current step.
 * @returns The generated zebra line.
 */
function genZebraLine(step: number): string {
  let zebra1Pos = (LINE_LENGTH - step) % LINE_LENGTH; // Initial position for the first zebra
  let zebra2Pos = (zebra1Pos + Math.floor(LINE_LENGTH / 2)) % LINE_LENGTH; // Ensure the second zebra starts from the opposite side

  // Generate the line with zebras
  let line = Array.from({ length: LINE_LENGTH }, (_, i) =>
    i === zebra1Pos || i === zebra2Pos ? "🦓" : "―"
  ).join("");

  return line;
}
/**
 * Gets the status of a temporary CLI token.
 *
 * @param cliTempToken - The temporary CLI token.
 * @returns The status of the temporary CLI token.
 */
async function getClITempTokenStatus(
  cliTempToken: string
): Promise<
  WaitingTorResolveCliTokenStatus | ResolvedCliTokenStatusWithUserToken
> {
  try {
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
  } catch (e) {
    console.error(e);
    throw new Error(
      `Failed to contact ${LIVESTACK_DASHBOARD_URL_ROOT}. ` + CONN_FAILED_MSG
    );
  }
}

/**
 * Schema for validating data as Buffer or Stream.
 */
export const fileOrBufferSchema = z.custom<Buffer | Stream>(
  (data): data is Buffer | Stream => {
    return data instanceof Buffer || data instanceof Stream;
  },
  "Data is not an instance of a Buffer or a Stream."
);
