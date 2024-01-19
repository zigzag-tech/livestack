import { Socket, io } from "socket.io-client";
import {
  RequestAndBindType,
  REQUEST_AND_BIND_CMD,
  JOB_INFO,
  JobInfoType,
} from "@livestack/shared/gateway-binding-types";

export class JobSocketIOConnection {
  public readonly jobId: string;
  public readonly inputKeys: string[];
  public readonly outputKeys: string[];
  public readonly socketIOClient: Socket;
  private readonly isConnDedicated: boolean;

  constructor({
    jobInfo: { jobId, inputKeys, outputKeys },
    socketIOClient,
    isConnDedicated,
  }: {
    jobInfo: JobInfoType;
    socketIOClient: Socket;
    isConnDedicated: boolean;
  }) {
    this.jobId = jobId;
    this.inputKeys = inputKeys;
    this.outputKeys = outputKeys;
    this.socketIOClient = socketIOClient;
    this.isConnDedicated = isConnDedicated;
  }

  public async feed<T>(data: T, key: string = "default") {
    // await getConnReadyPromise(this.socketIOClient);

    if (!this.inputKeys.includes(key)) {
      throw new Error(`Key ${key} not in inputKeys.`);
    }

    this.socketIOClient.emit(`feed:${this.jobId}/${key}`, data);
  }

  private subscribedOutputKeys: string[] = [];

  public async subToOutput<T>(
    key: string = "default",
    callback: (data: T) => void
  ) {
    // await getConnReadyPromise(this.socketIOClient);

    if (!this.outputKeys.includes(key)) {
      throw new Error(`Key ${key} not in outputKeys.`);
    }
    this.socketIOClient.on(`output:${this.jobId}/${key}`, callback);
    this.subscribedOutputKeys.push(key);
  }

  public async close() {
    this.socketIOClient.emit(`unbind:${this.jobId}`);
    for (const key of this.subscribedOutputKeys) {
      this.socketIOClient.off(`output:${this.jobId}/${key}`);
    }
    console.log(this.isConnDedicated);
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
  const { jobId, inputKeys, outputKeys } = await new Promise<{
    jobId: string;
    inputKeys: string[];
    outputKeys: string[];
  }>((resolve) => {
    conn.on(JOB_INFO, ({ inputKeys, outputKeys, jobId }: JobInfoType) => {
      resolve({ jobId, inputKeys, outputKeys });
    });
  });

  return new JobSocketIOConnection({
    jobInfo: { jobId, inputKeys, outputKeys },
    socketIOClient: conn,
    isConnDedicated: newClient,
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
