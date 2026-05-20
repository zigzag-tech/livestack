import { LiveEnv } from "@livestack/core";
import { Server as SocketIOServer } from "socket.io";
import { Server as HTTPServer } from "http";
import { LiveGatewayConn } from "./LiveGatewayConn";
import { SpecOrName, resolveUniqueSpec } from "@livestack/core";
import jwt from "jsonwebtoken";

const JWT_SECRET = "jwt_secret";
/**
 * Initializes the job binding for the LiveStack socket.io gateway.
 *
 * @param {Object} params - The parameters for initializing the job binding.
 * @param {HTTPServer} params.httpServer - The HTTP server instance.
 * @param {string} [params.socketPath="/livestack.socket.io"] - The path for the socket.io server.
 * @param {function} [params.onConnect] - Callback function to be called on a new connection.
 * @param {SpecOrName[]} params.allowedSpecsForBinding - List of allowed specs for binding.
 * @param {LiveEnv|null} [params.liveEnv] - The live environment instance.
 * @param {string} [params.authToken] - The authentication token for socket connections.
 * @returns {SocketIOServer} The initialized socket.io server instance.
 */
export function initJobBinding({
  httpServer,
  socketPath = "/livestack.socket.io",
  onConnect,
  allowedSpecsForBinding = [],
  liveEnv,
  authToken,
}: {
  httpServer: HTTPServer;
  socketPath?: string;
  onConnect?: (conn: LiveGatewayConn) => void;
  allowedSpecsForBinding: SpecOrName[];
  liveEnv?: LiveEnv | null;
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
      const suppliedToken = socket.handshake.auth?.token ||
        bearerTokenFromHeader(socket.handshake.headers.authorization);
      if (!suppliedToken) {
        return next(new Error("Missing auth token."));
      }
      if (suppliedToken === authToken) {
        return next();
      }
      jwt.verify(suppliedToken, JWT_SECRET, (err: Error | null, decoded: any) => {
        if (err || suppliedToken !== authToken) {
          console.log("Socket auth token verification failed:", err?.message);
          return next(new Error("Socket auth token verification failed."));
        }
        (socket as any).decoded = decoded;
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
      liveEnv,
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

function bearerTokenFromHeader(header: string | string[] | undefined): string | undefined {
  const value = Array.isArray(header) ? header[0] : header;
  const match = value?.match(/^Bearer\s+(.+)$/i);
  return match?.[1];
}
