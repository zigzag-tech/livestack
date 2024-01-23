import { Socket, io } from "socket.io-client";
import {
  RequestAndBindType,
  REQUEST_AND_BIND_CMD,
  JOB_INFO,
  JobInfoType,
  FEED,
  FeedParams,
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

  public async feed<T>(data: T, tag: string) {
    // await getConnReadyPromise(this.socketIOClient);
    const feedParams: FeedParams<T> = {
      data,
      tag,
    };
    this.socketIOClient.emit(FEED, feedParams);
  }

  private subscribedOutputKeys: string[] = [];

  public subToOutput<T>({ tag }: { tag: string }, callback: (data: T) => void) {
    // await getConnReadyPromise(this.socketIOClient);

    if (!this.availableOutputs.some((o) => o.key === tag)) {
      throw new Error(`Output of tag "${tag}" not in availableOutputs.`);
    }
    this.socketIOClient.on(`output:${this.jobId}/${tag}`, callback);
    this.subscribedOutputKeys.push(tag);

    const unsub = () => {
      this.socketIOClient.off(`output:${this.jobId}/${tag}`, callback);
      this.subscribedOutputKeys = this.subscribedOutputKeys.filter(
        (k) => k !== tag
      );
    };

    return unsub;
  }

  public async close() {
    this.socketIOClient.emit(`unbind:${this.jobId}`);
    for (const key of this.subscribedOutputKeys) {
      this.socketIOClient.off(`output:${this.jobId}/${key}`);
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
