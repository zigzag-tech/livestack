import { ZZEnv } from "@livestack/core";
import { Server as SocketIOServer } from "socket.io";
import { Server as HTTPServer } from "http";
import {
  SpecOrName,
  resolveUniqueSpec,
} from "@livestack/core/orchestrations/ZZWorkflow";
import { LiveGatewayConn } from "./LiveGatewayConn";

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
  allowedSpecsForBinding: SpecOrName[];
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
    const conn = new LiveGatewayConn({
      socket,
      allowedSpecsForBinding: allowedSpecsForBinding
        .map(resolveUniqueSpec)
        .map(({ spec, uniqueSpecLabel }) => ({
          specName: spec.name,
          uniqueSpecLabel,
        })),
      zzEnv,
    });
    if (onConnect) {
      onConnect(conn);
    }
    let disconnected = false;

    socket.on("disconnect", async () => {
      if (!disconnected) {
        disconnected = true;
      }
      console.info(`🦓 Socket client disconnected ${socket.id}.`);
    });

    socket.on("reconnect", async () => {
      disconnected = false;
      console.log("reconnected");
    });
  });
  console.info(
    `🦓 LiveStack socket.io gateway initiated on path ${socketPath}.`
  );
  return io;
}
