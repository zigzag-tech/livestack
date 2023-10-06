import Redis from "ioredis";

const PUBSUB_BY_ID: Record<string, { pub: Redis; sub: Redis }> = {};

const getPubSubClientsById = async ({
  queueId,
  projectId,
  hoseType,
}: {
  queueId: string;
  projectId: string;
  hoseType: "input" | "output";
}) => {
  const id = `msgq:${hoseType}:${projectId}--${queueId!}`;
  if (!PUBSUB_BY_ID[id]) {
    const sub = new Redis();
    await sub.subscribe(id, (err, count) => {
      if (err) {
        console.error("Failed to subscribe: %s", err.message);
      } else {
        // console.info(
        //   `getPubSubClientsById: subscribed successfully! This client is currently subscribed to ${count} channels.`
        // );
      }
    });
    const pub = new Redis();
    PUBSUB_BY_ID[id] = { sub, pub };
  }
  return { channelId: id, clients: PUBSUB_BY_ID[id] };
};

const _pub = async <T extends object>({
  message,
  queueId,
  messageId,
  hoseType,
  projectId,
}: {
  message: T;
  queueId: string;
  messageId: string;
  hoseType: "input" | "output";
  projectId: string;
}) => {
  const { channelId, clients } = await getPubSubClientsById({
    queueId,
    projectId,
    hoseType,
  });
  const addedMsg = await clients.pub.publish(
    channelId,
    customStringify({ message, messageId })
  );
  return addedMsg;
};

export const pubToJobInput = async <T extends object>({
  message,
  queueId,
  messageId,
  projectId,
}: {
  message: T;
  queueId: string;
  messageId: string;
  projectId: string;
}) => {
  return await _pub({
    message,
    projectId,
    queueId,
    messageId,
    hoseType: "input",
  });
};

export const pubToJobOutput = async <T extends object>({
  message,
  queueId,
  messageId,
  projectId,
}: {
  message: T;
  queueId: string;
  messageId: string;
  projectId: string;
}) => {
  return await _pub({
    projectId,
    message,
    queueId,
    messageId,
    hoseType: "output",
  });
};

const _sub = async <T>({
  queueId,
  processor,
  hoseType,
  projectId,
}: {
  queueId: string;
  processor: (message: T) => Promise<void>;
  hoseType: "input" | "output";
  projectId: string;
}) => {
  const { clients } = await getPubSubClientsById({
    queueId,
    projectId,
    hoseType,
  });

  clients.sub.on("message", async (channel, message) => {
    const { message: msg, messageId } = customParse(message);
    // console.log(`Received ${messageId} from ${channel}`);
    await processor(msg);
  });

  const unsub = async () => {
    await clients.sub.unsubscribe();
  };

  return {
    unsub,
  };
};

// TODO: make internal
export const subForJobInput = async <T>({
  queueId,
  processor,
  projectId,
}: {
  queueId: string;
  processor: (message: T) => Promise<void>;
  projectId: string;
}) => {
  return await _sub({ projectId, queueId, processor, hoseType: "input" });
};

export const subForJobOutput = async <T>({
  queueId,
  processor,
  projectId,
}: {
  queueId: string;
  processor: (message: T) => Promise<void>;
  projectId: string;
}) => {
  return await _sub({ projectId, queueId, processor, hoseType: "output" });
};

function customStringify(obj: any): string {
  function replacer(key: string, value: any): any {
    if (value instanceof Buffer) {
      return { type: "Buffer", data: value.toString("base64") };
    }
    return value;
  }
  return JSON.stringify(obj, replacer);
}

function customParse(json: string): any {
  function reviver(key: string, value: any): any {
    if (value && value.type === "Buffer") {
      return Buffer.from(value.data, "base64");
    }
    return value;
  }
  return JSON.parse(json, reviver);
}
