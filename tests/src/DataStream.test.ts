import { z } from "zod";
import { DataStream, DataStreamSubscriber, LiveEnv } from "@livestack/core";
import { getLogger } from "@livestack/core";
import { liveEnvP } from "./liveEnv";

async function testMain() {
  // set the global liveEnv
  LiveEnv.setGlobal(await liveEnvP);
  // Create a unique name for the stream
  // Configuration for LiveEnv

  // Define a simple message structure for testing
  const messageSchema = z.object({
    text: z.string(),
  });
  const streamName = "testStream-" + Date.now();

  // Create a new stream
  const stream = await DataStream.getOrCreate({
    uniqueName: streamName,
    def: messageSchema,

    logger: getLogger("test"),
  });

  // Subscribe to the stream
  const { valueObservable: observable, unsubscribe } =
    DataStreamSubscriber.subFromBeginning(stream);

  // Handle incoming messages
  observable.subscribe({
    next: (message) => {
      console.log("Received message:", message);
      // Perform assertions or checks here
    },
    error: (err) => console.error("Error:", err),
    complete: () => {
      console.log("Completed");
      process.exit(0);
    },
  });

  // Publish a message
  const testMessage = { text: "Hello, World!" };
  const chunkId = await stream.pub({
    message: testMessage,
    parentDatapoints: [],
  });
  console.log("Message published with ID:", chunkId);
  unsubscribe();
  process.exit();
}
if (require.main === module) {
  testMain();
}
