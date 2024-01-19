import { Socket, io } from "socket.io-client";
import {
  RequestAndBindType,
  REQUEST_AND_BIND_CMD,
  JOB_INFO,
  JobInfoType,
} from "@livestack/shared/gateway-binding-types";

export class JobSocketIOClient {
  public readonly specName: string;
  public readonly uniqueSpecLabel?: string;
  protected readonly socketIOClient: Socket;
  protected readonly connReadyPromise: Promise<void>;

  protected status: "not-initialized" | "working" = "not-initialized";
  protected jobInfo: {
    jobId: string;
    inputKeys: string[];
    outputKeys: string[];
  } | null = null;

  constructor({
    specName,
    uniqueSpecLabel,
    socketIOClient,
    socketIOURI,
    socketIOPath,
  }: RequestAndBindType & {
    socketIOClient?: Socket;
    socketIOURI?: string | null;
    socketIOPath?: string | null;
  }) {
    this.specName = specName;
    this.uniqueSpecLabel = uniqueSpecLabel;

    if (socketIOClient) {
      if (socketIOURI) {
        throw new Error("Cannot specify both serverBase and socketIOClient.");
      }
      this.socketIOClient = socketIOClient;
    } else {
      socketIOURI = socketIOURI || "";
      this.socketIOClient = io(socketIOURI, {
        autoConnect: true,
        path: socketIOPath || "/livestack.socket.io",
        transports: ["websocket", "polling"],
      });
    }
    this.connReadyPromise = new Promise((resolve, reject) => {
      if (this.socketIOClient.connected) {
        resolve();
      }
      this.socketIOClient.on("connected", () => {
        resolve();
      });
      this.socketIOClient.on("error", (err) => {
        console.error(err);
        reject(err);
      });
    });
  }

  protected async _ensureJobBinding() {
    if (this.status === "not-initialized") {
      this.status = "working";
      this.jobInfo = await this._requestAndBind();
    }
  }

  private async _requestAndBind() {
    const data: RequestAndBindType = {
      specName: this.specName,
      ...(this.uniqueSpecLabel
        ? { uniqueSpecLabel: this.uniqueSpecLabel }
        : {}),
    };
    await this.connReadyPromise;
    this.socketIOClient.emit(REQUEST_AND_BIND_CMD, data);
    return new Promise<{
      jobId: string;
      inputKeys: string[];
      outputKeys: string[];
    }>((resolve) => {
      this.socketIOClient.on(
        JOB_INFO,
        ({ inputKeys, outputKeys, jobId }: JobInfoType) => {
          resolve({ jobId, inputKeys, outputKeys });
        }
      );
    });
  }
  public async feed<T>(data: T, key: string = "default") {
    await this._ensureJobBinding();
    if (!this.jobInfo) {
      throw new Error("jobInfo is null");
    }
    if (!this.jobInfo.inputKeys.includes(key)) {
      throw new Error(`Key ${key} not in inputKeys.`);
    }

    this.socketIOClient.emit(`feed:${this.jobInfo.jobId}/${key}`, data);
  }

  public async subToOutput<T>(
    key: string = "default",
    callback: (data: T) => void
  ) {
    await this._ensureJobBinding();
    if (!this.jobInfo) {
      throw new Error("jobInfo is null");
    }
    if (!this.jobInfo.outputKeys.includes(key)) {
      throw new Error(`Key ${key} not in outputKeys.`);
    }

    this.socketIOClient.on(`output:${this.jobInfo.jobId}/${key}`, callback);
  }

  public async close() {
    await this.connReadyPromise;
    this.socketIOClient.close();
  }
}
