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
  CMD_UNSUB_TO_STREAM,
  UnbindParams,
  CMD_SUB_TO_STREAM,
} from "@livestack/shared";
import { requestAndGetResponse } from "./requestAndGetResponse";
import { z } from "zod";

export type StreamQuery = {
  type: "lastN";
  n: number;
};
export class JobSocketIOConnection {
  public readonly jobId: string;
  public readonly availableInputs: JobInfoType["availableInputs"];
  public readonly availableOutputs: JobInfoType["availableOutputs"];
  public readonly socketIOClient: Socket;
  private readonly isConnDedicated: boolean;
  public readonly specName: string;
  public readonly uniqueSpecLabel: string | undefined;
  public readonly jobOptions?: any;

  constructor({
    jobInfo: { jobId, availableInputs, availableOutputs },
    specName,
    uniqueSpecLabel,
    socketIOClient,
    isConnDedicated,
    jobOptions,
  }: {
    jobInfo: JobInfoType;
    specName: string;
    uniqueSpecLabel?: string;
    socketIOClient: Socket;
    isConnDedicated: boolean;
    jobOptions?: any;
  }) {
    this.jobId = jobId;
    this.socketIOClient = socketIOClient;
    this.isConnDedicated = isConnDedicated;
    this.availableInputs = availableInputs;
    this.availableOutputs = availableOutputs;
    this.jobOptions = jobOptions;

    this.specName = specName;
    this.uniqueSpecLabel = uniqueSpecLabel;

    this.socketIOClient.on("connect_error", (err) => {
      const error = new Error(
        "Connection failed due to authentication error: " + err.message
      );
      console.error("Connection failed:", err.message);
      throw error;
    });
  }

  private localObservablesByTag: Record<
    string,
    {
      observable: Observable<{
        data: any;
        tag: string;
        chunkId: string;
        timestamp: number;
      }>;
      subj: Subject<{
        data: any;
        tag: string;
        chunkId: string;
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
      jobId: this.jobId,
    };
    this.socketIOClient.emit(CMD_FEED, feedParams);
    if (!this.localObservablesByTag[tag]) {
      // create subject and observable
      const subj = new Subject<{
        data: T;
        tag: string;
        chunkId: string;
        timestamp: number;
      }>();
      const observable = subj.asObservable();
      this.localObservablesByTag[tag] = { subj, observable };
    }
    this.localObservablesByTag[tag].subj.next({
      data,
      tag,
      timestamp: Date.now(),
      chunkId: Math.random().toString(),
    });
  }

  private subscribedOutputKeys: string[] = [];

  public subToStream<T>(
    { tag, type }: { tag?: string; type: "input" | "output" },
    callback: (data: {
      data: T;
      tag: string;
      chunkId: string;
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
      requestAndGetResponse({
        req: {
          data: { jobId: this.jobId, tag, type: "output" },
          method: CMD_SUB_TO_STREAM,
        },
        conn: this.socketIOClient,
      });

      this.socketIOClient.on(`stream:${this.jobId}/${tag}`, callback);
      this.subscribedOutputKeys.push(tag);

      const unsub = async () => {
        requestAndGetResponse({
          req: {
            data: { jobId: this.jobId, tag, type: "output" },
            method: CMD_UNSUB_TO_STREAM,
          },
          conn: this.socketIOClient,
        });
        // this.socketIOClient.emit(CMD_UNSUB_TO_STREAM, {
        //   tag,
        //   jobId: this.jobId,
        //   type: "output",
        // });
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
          chunkId: string;
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
      this.socketIOClient.off("connect_error");
      this.socketIOClient.close();
    }
    this.closed = true;
  }
}

export interface ClientConnParams<P> {
  socketIOClient?: Socket;
  socketIOURI?: string | null;
  socketIOPath?: string | null;
  jobId?: string | null | undefined;
  jobOptions?: P;
  jobOptionsDef?: z.ZodType<P>;
}

const connCacheBySocketIOURIAndPath: Record<`${string}/${string}`, Socket> = {};

function getClient<P>({
  socketIOClient,
  socketIOURI,
  socketIOPath,
  authToken,
}: ClientConnParams<P> & { authToken?: String }) {
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

            auth: {
              token: authToken, // Pass the authentication token here
            },
          })
        : io({
            path: socketIOPath || "/livestack.socket.io",
            autoConnect: true,
            auth: {
              token: authToken, // Pass the authentication token here
            },
          });
    }
    return {
      newClient: false,
      conn: connCacheBySocketIOURIAndPath[socketIOURIAndPathKey],
    };
  }
}

export async function bindNewJobToSocketIO<P>({
  specName,
  uniqueSpecLabel,
  authToken,
  jobOptions,
  jobId: requestedJobId,
  ...connParams
}: Omit<RequestAndBindType, "requestIdentifier"> &
  ClientConnParams<P> & {
    authToken?: String;
  }): Promise<JobSocketIOConnection> {
  const { newClient, conn } = getClient({
    ...connParams,
    authToken, // Pass the authToken to getClient
  });

  // await getConnReadyPromise(conn!);
  const requestBindingData: RequestAndBindType = {
    specName,
    ...(uniqueSpecLabel ? { uniqueSpecLabel } : {}),
    jobId: requestedJobId,
    jobOptions,
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
    jobOptions,
  });
}
