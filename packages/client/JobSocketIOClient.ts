import { Observable, Subject } from "rxjs";
import { Socket, io } from "socket.io-client";
import {
  RequestAndBindType,
  REQUEST_AND_BIND_CMD,
  JOB_INFO,
  JobInfoType,
  FEED,
  FeedParams,
  UNBIND_CMD,
  UnbindParams,
} from "@livestack/shared/gateway-binding-types";

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
      observable: Observable<any>;
      subj: Subject<any>;
    }
  > = {};

  public async feed<T>(data: T, tag: string) {
    // await getConnReadyPromise(this.socketIOClient);
    const feedParams: FeedParams<T> = {
      data,
      tag,
    };
    this.socketIOClient.emit(FEED, feedParams);
    if (!this.localObservablesByTag[tag]) {
      // create subject and observable
      const subj = new Subject<T>();
      const observable = subj.asObservable();
      this.localObservablesByTag[tag] = { subj, observable };
    }
    this.localObservablesByTag[tag].subj.next(data);
  }

  private subscribedOutputKeys: string[] = [];

  public subToStream<T>({ tag }: { tag: string }, callback: (data: T) => void) {
    // await getConnReadyPromise(this.socketIOClient);
    if (this.availableOutputs.some((t) => t === tag)) {
      this.socketIOClient.on(`stream:${this.jobId}/${tag}`, callback);
      this.subscribedOutputKeys.push(tag);

      const unsub = () => {
        this.socketIOClient.off(`stream:${this.jobId}/${tag}`, callback);
        this.subscribedOutputKeys = this.subscribedOutputKeys.filter(
          (k) => k !== tag
        );
      };

      return unsub;
    } else if (this.availableInputs.some((t) => t === tag)) {
      // subscribe to local observable
      if (!this.localObservablesByTag[tag]) {
        const subj = new Subject<T>();
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
        `Tag "${tag}" not in available streams: [${[
          ...this.availableOutputs,
          ...this.availableInputs,
        ]
          .map((t) => `"${t}"`)
          .join(", ")}]`
      );
    }
  }

  public async close() {
    this.socketIOClient.emit(UNBIND_CMD, { jobId: this.jobId } as UnbindParams);
    for (const key of this.subscribedOutputKeys) {
      this.socketIOClient.off(`stream:${this.jobId}/${key}`);
    }
    this.subscribedOutputKeys = [];
    if (this.isConnDedicated) {
      this.socketIOClient.close();
    }
  }
}

export interface ClientConnParams {
  socketIOClient?: Socket;
  socketIOURI?: string | null;
  socketIOPath?: string | null;
}

function getClient({
  socketIOClient,
  socketIOURI,
  socketIOPath,
}: ClientConnParams) {
  // resolve as follows: if socketIOClient is provided, use it. Otherwise, if socketIOURI is provided, use it. Otherwise, use default (i.e. empty string)

  if (socketIOClient) {
    return { newClient: false, conn: socketIOClient };
  } else if (socketIOURI) {
    return {
      newClient: true,
      conn: io(socketIOURI, {
        path: socketIOPath || "/livestack.socket.io",
        autoConnect: true,
      }),
    };
  } else {
    return {
      newClient: true,
      conn: io({
        path: socketIOPath || "/livestack.socket.io",
        autoConnect: true,
      }),
    };
  }
}

export async function bindNewJobToSocketIO({
  specName,
  uniqueSpecLabel,
  ...connParams
}: RequestAndBindType & ClientConnParams): Promise<JobSocketIOConnection> {
  const { newClient, conn } = getClient(connParams);

  // await getConnReadyPromise(conn!);
  const requestBindingData: RequestAndBindType = {
    specName,
    ...(uniqueSpecLabel ? { uniqueSpecLabel } : {}),
  };
  conn.emit(REQUEST_AND_BIND_CMD, requestBindingData);
  const { jobId, availableInputs, availableOutputs } = await new Promise<{
    jobId: string;
    availableOutputs: JobInfoType["availableOutputs"];
    availableInputs: JobInfoType["availableInputs"];
  }>((resolve) => {
    conn.on(
      JOB_INFO,
      ({ availableInputs, availableOutputs, jobId }: JobInfoType) => {
        resolve({ jobId, availableInputs, availableOutputs });
      }
    );
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
