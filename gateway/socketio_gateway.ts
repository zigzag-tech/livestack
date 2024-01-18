import { SpecOrName } from "@livestack/shared/ZZJobSpecBase";
import { Subscription } from "rxjs";
import { ZZEnv, ZZJobSpec } from "@livestack/core";
import { Socket, Server as SocketIOServer } from "socket.io";
import { Server as HTTPServer } from "http";
import { resolveUniqueSpec } from "@livestack/core/jobs/ZZJobSpec";

export function setupJobBindingGateway({
  httpServer,
  socketPath = "/livestack.socket.io",
  onConnect,
  allowedSpecsForBinding = [],
  zzEnv,
}: {
  httpServer: HTTPServer;
  socketPath?: string;
  onConnect?: (conn: LiveGatewayConn) => void;
  allowedSpecsForBinding?: SpecOrName[];
  zzEnv?: ZZEnv | null;
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
        allowedSpecsForBinding: allowedSpecsForBinding.map(
          (s) => resolveUniqueSpec(s).specName
        ),
        zzEnv,
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
    jobOptions?: P
  ) => {
    const { input, output, jobId } = await jobSpec.enqueueJob({ jobOptions });
    this.socket.emit("job_info", {
      jobId,
      inputKeys: input.keys,
      outputKeys: output.keys,
    });
    for (const key of input.keys) {
      this.socket.on(
        `feed:${jobId}/${String(key)}`,
        async (data: IMap[typeof key]) => {
          try {
            await input.byKey(key).feed(data);
          } catch (err) {
            console.error(err);
          }
        }
      );
    }

    let subs: Subscription[] = [];

    for (const key of output.keys) {
      const sub = output.byKey(key).valueObservable.subscribe((data) => {
        this.socket.emit(`output:${jobId}/${String(key)}`, data);
      });
      subs.push(sub);
    }

    this.onDisconnect(() => {
      for (const key of input.keys) {
        try {
          input.byKey(key).terminate();
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
