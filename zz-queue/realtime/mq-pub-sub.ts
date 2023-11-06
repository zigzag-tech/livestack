import Redis, { RedisOptions } from "ioredis";
import { ProjectConfig } from "../config-factory/config-defs";
import { Observable } from "rxjs";
import { v4 } from "uuid";
const PUBSUB_BY_ID: Record<string, { pub: Redis; sub: Redis }> = {};

export class PubSubFactory {
  private _projectConfig: ProjectConfig;
  private _redisConfig: RedisOptions;
  private _queueId: string;

  constructor(
    projectConfig: ProjectConfig,
    redisConfig: RedisOptions,
    queueId: string
  ) {
    this._projectConfig = projectConfig;
    this._redisConfig = redisConfig;
    this._queueId = queueId;
  }

  public async pubToJobInput<T extends object>({
    message,
    messageId,
  }: {
    message: T;
    messageId: string;
  }) {
    return await this._pub({
      message,
      messageId,
      hoseType: "input",
    });
  }

  public async pubToJobOutput<T>({
    message,
    messageId,
  }: {
    message: T;
    messageId: string;
  }) {
    return await this._pub({
      message,
      messageId,
      hoseType: "output",
    });
  }

  public subForJobInput<T>({ processor }: { processor: (message: T) => void }) {
    return this._sub({
      processor,
      hoseType: "input",
    });
  }

  public async subForJobOutput<T>({
    processor,
  }: {
    processor: (message: T) => void;
  }) {
    return await this._sub({
      processor,
      hoseType: "output",
    });
  }

  private getPubSubClientsById({
    queueId,
    hoseType,
  }: {
    queueId: string;
    hoseType: "input" | "output";
  }) {
    const id = `msgq:${hoseType}:${this._projectConfig.projectId}--${queueId!}`;
    if (!PUBSUB_BY_ID[id]) {
      const sub = new Redis(this._redisConfig);
      sub.subscribe(id, (err, count) => {
        if (err) {
          console.error("Failed to subscribe: %s", err.message);
        } else {
          // console.info(
          //   `getPubSubClientsById: subscribed successfully! This client is currently subscribed to ${count} channels.`
          // );
        }
      });
      const pub = new Redis(this._redisConfig);
      PUBSUB_BY_ID[id] = { sub, pub };
    }
    return { channelId: id, clients: PUBSUB_BY_ID[id] };
  }

  private async _pub<T>({
    message,
    messageId,
    hoseType,
  }: {
    message: T;
    messageId: string;
    hoseType: "input" | "output";
  }) {
    const { channelId, clients } = await this.getPubSubClientsById({
      queueId: this._queueId,
      hoseType,
    });
    const addedMsg = await clients.pub.publish(
      channelId,
      customStringify({ message, messageId })
    );
    return addedMsg;
  }

  private _sub<T>({
    processor,
    hoseType,
  }: {
    processor: (message: T) => void;
    hoseType: "input" | "output";
  }) {
    const { clients } = this.getPubSubClientsById({
      queueId: this._queueId,
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
  }
}

// TODO: make internal

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

export function sequentialInputFactory<I>({
  pubSubFactory,
}: {
  pubSubFactory: PubSubFactory;
}) {
  let inputGenerator: ReturnType<typeof generateInputs>;
  let inputSubObservable: Observable<I>;
  const nextInput = async () => {
    if (!inputSubObservable) {
      inputSubObservable = new Observable<I>((subscriber) => {
        const { unsub } = pubSubFactory.subForJobInput<I>({
          processor: async (v) => {
            subscriber.next(v);
          },
        });
        return {
          unsubscribe: () => {
            unsub();
          },
        };
      });
    }

    const { value, done } = await inputGenerator.next();
    if (done) {
      throw new Error("Observable completed");
    }
    return value;
  };

  const generateInputs = async function* (): AsyncGenerator<I, void, unknown> {
    let resolve: ((value: I) => void) | null = null;
    const promiseQueue: I[] = [];

    const subscription = inputSubObservable.subscribe({
      next(value: I) {
        if (resolve) {
          resolve(value);
          resolve = null;
        } else {
          promiseQueue.push(value);
        }
      },
      error(err) {
        throw err;
      },
    });

    try {
      while (true) {
        if (promiseQueue.length > 0) {
          yield promiseQueue.shift()!;
        } else {
          yield new Promise<I>((res) => {
            resolve = res;
          });
        }
      }
    } finally {
      subscription.unsubscribe();
    }
  };

  return { nextInput };
}

export function sequentialOutputFactory<O>({
  pubSubFactory,
}: {
  pubSubFactory: PubSubFactory;
}) {
  const emitOutput = async (o: O) => {
    pubSubFactory.pubToJobOutput({
      message: o,
      messageId: v4(),
    });
  };
  return { emitOutput };
}
