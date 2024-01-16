import { ZZJobSpec, ZZWorkflowSpec } from '@livestack/core';
import { Socket, Server as SocketIOServer } from "socket.io";
import { Server as HTTPServer } from "http";

export function setupSocketIOGateway({
  httpServer,
  socketPath = "/livestack.socket.io",
  onConnect,
}: {
  httpServer: HTTPServer;
  socketPath?: string;
  onConnect?: (conn: LiveGatewayConn) => void;
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
      const conn = new LiveGatewayConn(socket);
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
  
  constructor(socket: Socket) {
    this.socket = socket;
  }

  public onDisconnect = async (cb: () => void) => {
    this.socket.on("disconnect", cb);
  }

  public bind = async <P,IMap, OMap>(jobSpec: ZZJobSpec<P, IMap, OMap>, jobParams?: P) => {
   const {inputs, outputs, jobId} = await jobSpec.enqueueJob({jobParams  });
   this.onDisconnect(() => {
      for (const key of inputs.keys) {
        try {
          inputs.byKey(key).terminate();
        } catch (err) {
          console.error(err);
        }
      }
   });
  }
}
