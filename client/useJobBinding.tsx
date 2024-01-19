import { Socket, io } from "socket.io-client";
import { useRef, useState } from "react";
import useDeepCompareEffect from "use-deep-compare-effect";
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
  protected readonly readyPromise: Promise<void>;
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
    this.readyPromise = new Promise((resolve, reject) => {
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
    await this.readyPromise;
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
}

export class InputBindingClient extends JobSocketIOClient {
  public readonly inputKeys: string[];
  constructor({
    specName,
    uniqueSpecLabel,
    socketIOClient,
    socketIOURI,
    socketIOPath,
  }: {
    specName: string;
    uniqueSpecLabel?: string;
    socketIOClient?: Socket;
    socketIOURI?: string | null;
    socketIOPath?: string | null;
  }) {
    super({
      specName,
      uniqueSpecLabel,
      socketIOClient,
      socketIOURI,
      socketIOPath,
    });
    this.inputKeys = [];
  }

  public async feedInput<T>(data: T, key: string = "default") {
    await this._ensureJobBinding();
    if (!this.jobInfo) {
      throw new Error("jobInfo is null");
    }
    if (!this.inputKeys.includes(key)) {
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

    this.socketIOClient.on(`output:${this.jobInfo.jobId}/${key}`, callback);
  }
}

export function useJobBinding({
  serverBase,
  socketPath = "/livestack.socket.io",
  specName,
  outputsToWatch = [{ key: "default", mode: "replace" }],
}: {
  serverBase?: string | null;
  socketPath?: string;
  specName: string;
  outputsToWatch?: {
    key?: string;
    mode: "append" | "replace";
  }[];
}) {
  const clientRef = useRef<Socket>();

  const jobInfoRef = useRef<
    | {
        jobId: string;
        inputKeys: string[];
        outputKeys: string[];
      }
    | "not-initialized"
    | "working"
  >("not-initialized");

  const [outputsByKey, setOutputsByKey] = useState<{
    [key: string]: any;
  }>({});

  useDeepCompareEffect(() => {
    if (!clientRef.current) {
      if (serverBase) {
        clientRef.current = io(serverBase, {
          autoConnect: true,
          path: socketPath,
          transports: ["websocket", "polling"],
        });
      } else {
        clientRef.current = io({
          autoConnect: true,
          path: socketPath,
          transports: ["websocket", "polling"],
        });

      }
    }
    const client = clientRef.current;
    client.on("error", (err) => {
      console.error(err);
      console.error(
        "Error trying to connect to livestack gateway. Did you forget to set serverBase to the server hosting LiveStack gateway?"
      );
    });
    if (jobInfoRef.current === "not-initialized") {
      jobInfoRef.current = "working";
      client.emit("request_and_bind", { specName });
      client.on(JOB_INFO, ({ inputKeys, outputKeys, jobId }: JobInfoType) => {
        jobInfoRef.current = { jobId, inputKeys, outputKeys };
        for (const { mode, key = "default" } of outputsToWatch) {
          client.on(`output:${jobId}/${key}`, (data: any) => {
            // TODO: `data` data structure needs to be corrected. Not always an object.
            const timeStamped = { ...data, _timeStamp: Date.now() };
            if (mode === "replace") {
              setOutputsByKey((prev) => ({ ...prev, [key]: timeStamped }));
            } else if (mode === "append") {
              setOutputsByKey((prev) => ({
                ...prev,
                [key]: [...(prev[key] || []), timeStamped],
              }));
            }
          });
        }
      });
    }
    return () => {
      if (
        jobInfoRef.current !== "not-initialized" &&
        jobInfoRef.current !== "working"
      ) {
        client.emit("unbind", { specName, jobId: jobInfoRef.current?.jobId });
      }
    };
  }, [specName, outputsToWatch]);

  const feed = async <T,>(data: T, key: string = "default") => {
    if (typeof jobInfoRef.current === "string") {
      throw new Error("Background job not yet running.");
    }
    if (!jobInfoRef.current.inputKeys.includes(key)) {
      throw new Error(`Key ${key} not in inputKeys.`);
    }

    if (!clientRef.current) {
      throw new Error("clientRef.current is null");
    }
    clientRef.current.emit(`feed:${jobInfoRef.current.jobId}/${key}`, data);
  };

  return { jobInfo: jobInfoRef.current, feed, outputsByKey };
}
