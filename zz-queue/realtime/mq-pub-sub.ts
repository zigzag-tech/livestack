import Redis, { RedisOptions } from "ioredis";
import { ProjectConfig } from "../config-factory/config-defs";
import { Observable } from "rxjs";
import { v4 } from "uuid";
const PUBSUB_BY_ID: Record<string, { pub: Redis; sub: Redis }> = {};

export class PubSubFactory<T> {
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

  public async pubToJob({
    message,
    messageId,
    type,
  }: {
    message: T;
    messageId: string;
    type: "input" | "output";
  }) {
    return await this._pub({
      message,
      messageId,
      hoseType: type,
    });
  }

  public async pubToJobOutput({
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

  public subForJob({
    processor,
    type,
  }: {
    type: "input" | "output";
    processor: (message: T) => void;
  }) {
    return this._sub({
      processor,
      hoseType: type,
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

    // console.log("pubbing", channelId);

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
    const { clients, channelId } = this.getPubSubClientsById({
      queueId: this._queueId,
      hoseType,
    });

    // console.log("sub to", channelId);

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

export function sequentialFactory<T>({
  pubSubFactory,
}: {
  pubSubFactory: PubSubFactory<T>;
}) {
  let valueGenerator: ReturnType<typeof generateValues>;
  let _valueObservable: Observable<T>;
  const ensureValueObservable = () => {
    if (!_valueObservable) {
      _valueObservable = new Observable<T>((subscriber) => {
        const { unsub } = pubSubFactory.subForJob({
          type: "input",
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
      valueGenerator = generateValues();
    }
    return _valueObservable;
  };
  const nextValue = async () => {
    ensureValueObservable();

    const { value, done } = await valueGenerator.next();
    if (done) {
      throw new Error("Observable completed");
    }
    return value;
  };

  const generateValues = async function* (): AsyncGenerator<T, void, unknown> {
    let resolve: ((value: T) => void) | null = null;
    const promiseQueue: T[] = [];
    const subscription = ensureValueObservable().subscribe({
      next(value: T) {
        // console.log("vvvvvvalue", value);
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
          yield new Promise<T>((res) => {
            resolve = res;
          });
        }
      }
    } finally {
      subscription.unsubscribe();
    }
  };

  const emit = async (o: T) => {
    pubSubFactory.pubToJobOutput({
      message: o,
      messageId: v4(),
    });
  };

  return {
    nextValue,
    emit,
    get valueObsrvable() {
      return ensureValueObservable();
    },
  };
}
