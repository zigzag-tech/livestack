import { Observable, Subject } from "rxjs";
import { Socket, io } from "socket.io-client";
import {
  RequestAndBindType,
  REQUEST_AND_BIND_CMD,
  MSG_JOB_INFO,
  JobInfoType,
  CMD_FEED,
  FeedParams,
  CMD_UNBIND,
  UnbindParams,
} from "@livestack/shared/gateway-binding-types";
import { requestAndGetResponse } from "./requestAndGetResponse";

export class JobSocketIOConnection {
  public readonly jobId: string;
  public readonly availableInputs: JobInfoType["availableInputs"];
  public readonly availableOutputs: JobInfoType["availableOutputs"];
  public readonly socketIOClient: Socket;
  private readonly isConnDedicated: boolean;
  public readonly specName: string;
  public readonly uniqueSpecLabel: string | undefined;

  constructor({
    jobInfo: { jobId, availableInputs, availableOutputs },
    specName,
    uniqueSpecLabel,
    socketIOClient,
    isConnDedicated,
  }: {
    jobInfo: JobInfoType;
    specName: string;
    uniqueSpecLabel?: string;
    socketIOClient: Socket;
    isConnDedicated: boolean;
  }) {
    this.jobId = jobId;
    this.socketIOClient = socketIOClient;
    this.isConnDedicated = isConnDedicated;
    this.availableInputs = availableInputs;
    this.availableOutputs = availableOutputs;

    this.specName = specName;
    this.uniqueSpecLabel = uniqueSpecLabel;
  }

  private localObservablesByTag: Record<
    string,
    {
      observable: Observable<{
        data: any;
        tag: string;
        messageId: string;
        timestamp: number;
      }>;
      subj: Subject<{
        data: any;
        tag: string;
        messageId: string;
        timestamp: number;
      }>;
    }
  > = {};

  public async feed<T>(data: T, tag?: string) {
    // await getConnReadyPromise(this.socketIOClient);
    if (!tag) {
      if (this.availableInputs.length > 1) {
        throw new Error(
          ` $Ambiguous input for spec "${
            this.specName
          }"; found more than two with tags [${this.availableInputs.join(
            ", "
          )}]. \nPlease specify which one to use with "output(tagName)".`
        );
      }
      tag = this.availableInputs[0];
    }
    const feedParams: FeedParams<string, T> = {
      data,
      tag,
    };
    this.socketIOClient.emit(CMD_FEED, feedParams);
    if (!this.localObservablesByTag[tag]) {
      // create subject and observable
      const subj = new Subject<{
        data: T;
        tag: string;
        messageId: string;
        timestamp: number;
      }>();
      const observable = subj.asObservable();
      this.localObservablesByTag[tag] = { subj, observable };
    }
    this.localObservablesByTag[tag].subj.next({
      data,
      tag,
      timestamp: Date.now(),
      messageId: Math.random().toString(),
    });
  }

  private subscribedOutputKeys: string[] = [];

  public subToStream<T>(
    { tag, type }: { tag?: string; type: "input" | "output" },
    callback: (data: {
      data: T;
      tag: string;
      messageId: string;
      timestamp: number;
    }) => void
  ) {
    // await getConnReadyPromise(this.socketIOClient);
    if (!tag) {
      if (this.availableOutputs.length > 1) {
        throw new Error(
          ` $Ambiguous output for spec "${
            this.specName
          }"; found more than two with tags [${this.availableOutputs.join(
            ", "
          )}]. \nPlease specify which one to use with "input(tagName)".`
        );
      }
      tag = this.availableOutputs[0];
    }
    if (type === "output" && this.availableOutputs.some((t) => t === tag)) {
      this.socketIOClient.on(`stream:${this.jobId}/${tag}`, callback);
      this.subscribedOutputKeys.push(tag);

      const unsub = () => {
        this.socketIOClient.off(`stream:${this.jobId}/${tag}`, callback);
        this.subscribedOutputKeys = this.subscribedOutputKeys.filter(
          (k) => k !== tag
        );
      };

      return unsub;
    } else if (
      type === "input" &&
      this.availableInputs.some((t) => t === tag)
    ) {
      // subscribe to local observable
      if (!this.localObservablesByTag[tag]) {
        const subj = new Subject<{
          data: T;
          tag: string;
          messageId: string;
          timestamp: number;
        }>();
        const observable = subj.asObservable();
        this.localObservablesByTag[tag] = { subj, observable };
      }
      const sub =
        this.localObservablesByTag[tag].observable.subscribe(callback);
      const unsub = () => {
        sub.unsubscribe();
      };
      return unsub;
    } else {
      throw new Error(
        `Tag "${tag}" of type ${type} not in available streams: [${[
          ...this.availableOutputs,
          ...this.availableInputs,
        ]
          .map((t) => `"${t}"`)
          .join(", ")}]`
      );
    }
  }

  private closed = false;

  public async close() {
    if (this.closed) return;
    this.socketIOClient.emit(CMD_UNBIND, { jobId: this.jobId } as UnbindParams);
    for (const key of this.subscribedOutputKeys) {
      this.socketIOClient.off(`stream:${this.jobId}/${key}`);
    }
    this.subscribedOutputKeys = [];
    if (this.isConnDedicated) {
      this.socketIOClient.close();
    }
    this.closed = true;
  }
}

export interface ClientConnParams {
  socketIOClient?: Socket;
  socketIOURI?: string | null;
  socketIOPath?: string | null;
}

const connCacheBySocketIOURIAndPath: Record<`${string}/${string}`, Socket> = {};

function getClient({
  socketIOClient,
  socketIOURI,
  socketIOPath,
}: ClientConnParams) {
  // resolve as follows: if socketIOClient is provided, use it. Otherwise, if socketIOURI is provided, use it. Otherwise, use default (i.e. empty string)

  if (socketIOClient) {
    return { newClient: false, conn: socketIOClient };
  } else {
    const socketIOURIAndPathKey = `${socketIOURI}/${
      socketIOPath || ""
    }` as const;
    if (!connCacheBySocketIOURIAndPath[socketIOURIAndPathKey]) {
      connCacheBySocketIOURIAndPath[socketIOURIAndPathKey] = socketIOURI
        ? io(socketIOURI, {
            path: socketIOPath || "/livestack.socket.io",
            autoConnect: true,
          })
        : io({
            path: socketIOPath || "/livestack.socket.io",
            autoConnect: true,
          });
    }
    return {
      newClient: false,
      conn: connCacheBySocketIOURIAndPath[socketIOURIAndPathKey],
    };
  }
}

export async function bindNewJobToSocketIO({
  specName,
  uniqueSpecLabel,
  ...connParams
}: Omit<RequestAndBindType, "requestIdentifier"> &
  ClientConnParams): Promise<JobSocketIOConnection> {
  const { newClient, conn } = getClient(connParams);

  // await getConnReadyPromise(conn!);
  const requestBindingData: RequestAndBindType = {
    specName,
    ...(uniqueSpecLabel ? { uniqueSpecLabel } : {}),
  };
  const { jobId, availableInputs, availableOutputs } =
    await requestAndGetResponse<RequestAndBindType, JobInfoType>({
      req: { data: requestBindingData, method: REQUEST_AND_BIND_CMD },
      res: { method: MSG_JOB_INFO },
      conn,
    });

  return new JobSocketIOConnection({
    jobInfo: { jobId, availableInputs, availableOutputs },
    socketIOClient: conn,
    isConnDedicated: newClient,
    specName,
    uniqueSpecLabel,
  });
}

function getConnReadyPromise(conn: Socket) {
  return new Promise<void>((resolve, reject) => {
    if (conn.connected) {
      resolve();
    }

    conn.on("connection", () => {
      resolve();
    });
    conn.on("error", (err) => {
      console.error(err);
      reject(err);
    });
  });
}
