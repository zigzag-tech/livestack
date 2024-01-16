import { Subscription } from "rxjs";
import { ZZEnv, ZZJobSpec } from "@livestack/core";
import { Socket, Server as SocketIOServer } from "socket.io";
import { Server as HTTPServer } from "http";

export function setupSocketIOGateway({
  httpServer,
  socketPath = "/livestack.socket.io",
  onConnect,
  allowedSpecsForBinding = [],
}: {
  httpServer: HTTPServer;
  socketPath?: string;
  onConnect?: (conn: LiveGatewayConn) => void;
  allowedSpecsForBinding?: string[];
}) {
  const io = new SocketIOServer(httpServer, {
    path: socketPath,
    cors: {
      origin: "*", // Adjust according to your needs for security
      methods: ["GET", "POST"],
    },
  });

  io.on("connection", async (socket) => {
    console.info(`🦓 Socket client connected: ${socket.id}.`);
    if (onConnect) {
      const conn = new LiveGatewayConn({
        socket,
        allowedSpecsForBinding,
      });
      onConnect(conn);
    }
    let disconnected = false;

    socket.on("disconnect", async () => {
      if (!disconnected) {
        disconnected = true;
      }
      console.info(`🦓 Socket client disconnected  ${socket.id}.`);
    });

    socket.on("reconnect", async () => {
      disconnected = false;
      console.log("reconnected");
    });
  });
  console.info("🦓 LiveStack socket.io gateway initiated.");
  return io;
}

class LiveGatewayConn {
  socket: Socket;
  private readonly allowedSpecsForBinding: string[];
  zzEnv: ZZEnv;

  constructor({
    zzEnv,
    socket,
    allowedSpecsForBinding = [],
  }: {
    zzEnv?: ZZEnv | null;
    socket: Socket;
    allowedSpecsForBinding?: string[];
  }) {
    this.socket = socket;
    this.allowedSpecsForBinding = allowedSpecsForBinding;
    zzEnv = zzEnv || ZZEnv.global();
    if (!zzEnv) {
      throw new Error("ZZEnv not found.");
    }
    this.zzEnv = zzEnv;

    this.socket.on(
      "request_and_bind",
      async ({ specName }: { specName: string }) => {
        if (!this.allowedSpecsForBinding.includes(specName)) {
          throw new Error(
            `Spec name ${specName} not allowed for binding to socket.`
          );
        }
        const spec = ZZJobSpec.lookupByName(specName);
        await this.bindToNewJob(spec);
      }
    );
  }

  public onDisconnect = async (cb: () => void) => {
    this.socket.on("disconnect", cb);
  };

  public bindToNewJob = async <P, IMap, OMap>(
    jobSpec: ZZJobSpec<P, IMap, OMap>,
    jobParams?: P
  ) => {
    const { inputs, outputs, jobId } = await jobSpec.enqueueJob({ jobParams });
    this.socket.emit("jobId", jobId);
    for (const key of inputs.keys) {
      this.socket.on(
        `feed:${jobId}/${String(key)}`,
        async (data: IMap[typeof key]) => {
          try {
            await inputs.byKey(key).feed(data);
          } catch (err) {
            console.error(err);
          }
        }
      );
    }

    let subs: Subscription[] = [];

    for (const key of outputs.keys) {
      const sub = outputs.byKey(key).valueObservable.subscribe((data) => {
        this.socket.emit(`output:${jobId}/${String(key)}`, data);
      });
      subs.push(sub);
    }

    this.onDisconnect(() => {
      for (const key of inputs.keys) {
        try {
          inputs.byKey(key).terminate();
        } catch (err) {
          console.error(err);
        }
      }

      for (const sub of subs) {
        sub.unsubscribe();
      }
    });
  };
}
