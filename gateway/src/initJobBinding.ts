import { ZZEnv } from "@livestack/core";
import { Server as SocketIOServer } from "socket.io";
import { Server as HTTPServer } from "http";
import { LiveGatewayConn } from "./LiveGatewayConn";
import { SpecOrName, resolveUniqueSpec } from "@livestack/core";
import jwt from 'jsonwebtoken';

const JWT_SECRET = 'jwt_secret';
export function initJobBinding({
  httpServer,
  socketPath = "/livestack.socket.io",
  onConnect,
  allowedSpecsForBinding = [],
  zzEnv,
  authToken,
}: {
  httpServer: HTTPServer;
  socketPath?: string;
  onConnect?: (conn: LiveGatewayConn) => void;
  allowedSpecsForBinding: SpecOrName[];
  zzEnv?: ZZEnv | null;
  authToken?: string;
}) {
  const io = new SocketIOServer(httpServer, {
    path: socketPath,
    cors: {
      origin: "*", // Adjust according to your needs for security
      methods: ["GET", "POST"],
    },
  });
  io.use((socket, next) => {
    if (authToken) {
      jwt.verify(authToken, JWT_SECRET, (err: Error | null, decoded: any) => {
        if (err) {
          console.log('Global authToken verification failed:', err.message);
          return next(new Error('Global authToken verification failed.'));
        }
        console.log('Global authToken verified successfully');
        (socket as any).decoded = decoded; // Attach decoded information to the socket
        return next();
      });
    } else {
      return next();
    }
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
