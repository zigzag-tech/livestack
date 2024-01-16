import { Server as SocketIOServer } from "socket.io";
import { Server as HTTPServer } from "http";

export function setupSocketIOGateway(
  httpServer: HTTPServer,
  socketPath: string = "/livestack.socket.io"
) {
  const io = new SocketIOServer(httpServer, {
    path: socketPath,
    cors: {
      origin: "*", // Adjust according to your needs for security
      methods: ["GET", "POST"],
    },
  });

  io.on("connection", async (socket) => {
    console.info("connected");

    let disconnected = false;

    socket.on("disconnect", async () => {
      if (disconnected) {
      }
    });

    socket.on("reconnect", async () => {
      disconnected = false;
      console.log("reconnected");
    });
  });
  console.info("🦓 LiveStack socket.io gateway initiated.");
  return io;
}

function b64decode(encoded: string): Float32Array {
  // Decode the base64 string to a binary string
  let binaryString = atob(encoded);

  // Create a buffer to hold the bytes
  let bytes = new Uint8Array(binaryString.length);

  // Convert the binary string to bytes
  for (let i = 0; i < binaryString.length; i++) {
    bytes[i] = binaryString.charCodeAt(i);
  }

  // Convert the bytes back to a Float32Array
  return new Float32Array(bytes.buffer);
}
