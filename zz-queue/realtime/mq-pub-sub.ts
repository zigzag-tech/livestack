import Redis, { RedisOptions } from "ioredis";
import { ProjectConfig } from "../config-factory/config-defs";
import { BehaviorSubject, Observable, Subscriber, Subscription } from "rxjs";
import { v4 } from "uuid";
const PUBSUB_BY_ID: Record<string, { pub: Redis; sub: Redis }> = {};

export class PubSubFactory<T> {
  private _projectConfig: ProjectConfig;
  private _redisConfig: RedisOptions;
  private _queueId: string;
  private _valueObservable: Observable<T> | null = null;
  private _type: "input" | "output";

  private ensureValueObservable() {
    if (!this._valueObservable) {
      this._valueObservable = new Observable<T>((subscriber) => {
        const { unsub } = this.subForJob({
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

    return this._valueObservable;
  }

  public async emitValue(o: T) {
    this.pubToJob({
      message: o,
      messageId: v4(),
    });
  }

  get valueObsrvable() {
    return this.ensureValueObservable();
  }

  constructor(
    type: "input" | "output",
    projectConfig: ProjectConfig,
    redisConfig: RedisOptions,
    queueId: string
  ) {
    this._projectConfig = projectConfig;
    this._redisConfig = redisConfig;
    this._queueId = queueId;
    this._type = type;

    this.ensureValueObservable();
  }

  public async pubToJob({
    message,
    messageId,
  }: {
    message: T;
    messageId: string;
  }) {
    return await this._pub({
      message,
      messageId,
    });
  }

  public subForJob({ processor }: { processor: (message: T) => void }) {
    return this._sub({
      processor,
    });
  }

  private getPubSubClientsById({ queueId }: { queueId: string }) {
    const id = `msgq:${this._projectConfig.projectId}--${queueId!}/${
      this._type
    }`;
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
  }: {
    message: T;
    messageId: string;
  }) {
    const { channelId, clients } = await this.getPubSubClientsById({
      queueId: this._queueId,
    });

    // console.log("pubbing", channelId);

    const addedMsg = await clients.pub.publish(
      channelId,
      customStringify({ message, messageId })
    );
    return addedMsg;
  }

  private _sub<T>({ processor }: { processor: (message: T) => void }) {
    const { clients, channelId } = this.getPubSubClientsById({
      queueId: this._queueId,
    });

    // console.log("sub to", channelId);

    clients.sub.on("message", async (channel, message) => {
      const { message: msg, messageId } = customParse(message);
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

export class PubSubFactoryWithNextValueGenerator<T> extends PubSubFactory<T> {
  nextValue: () => Promise<T>;
  constructor(
    type: "input" | "output",
    projectConfig: ProjectConfig,
    redisConfig: RedisOptions,
    queueId: string
  ) {
    super(type, projectConfig, redisConfig, queueId);
    const { nextValue } = createLazyNextValueGenerator(this.valueObsrvable);
    this.nextValue = nextValue;
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

export function createLazyNextValueGenerator<T>(observable: Observable<T>) {
  let resolve: ((value: T) => void) | null = null;
  const promiseQueue: T[] = [];

  let subscription: Subscription;

  let _valueGenerator: AsyncGenerator<T>;
  const nextValue = async () => {
    if (!_valueGenerator) {
      subscription = observable.subscribe({
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
      const generateValues = async function* (): AsyncGenerator<
        T,
        void,
        unknown
      > {
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
      _valueGenerator = generateValues();
    }

    const { value, done } = await _valueGenerator!.next();
    if (done) {
      throw new Error("Observable completed");
    }
    return value;
  };

  return { nextValue };
}

export function createTrackedObservable<T>(observable: Observable<T>) {
  let subscriberCount = 0;
  const subscriberCountSubject = new BehaviorSubject<number>(subscriberCount);

  const trackedObservable = new Observable<T>((subscriber: Subscriber<T>) => {
    // Increment subscriber count
    subscriberCount++;
    subscriberCountSubject.next(subscriberCount);
    // console.log(`Subscribers: ${subscriberCount}`);

    // Subscribe to the original observable
    const subscription: Subscription = observable.subscribe({
      next: (value: T) => subscriber.next(value),
      error: (err: any) => subscriber.error(err),
      complete: () => subscriber.complete(),
    });

    // Return the teardown logic
    return () => {
      // Decrement subscriber count
      subscriberCount--;
      subscriberCountSubject.next(subscriberCount);
      // console.log(`Subscribers: ${subscriberCount}`);

      // Unsubscribe from the original observable
      subscription.unsubscribe();
    };
  });

  const subscriberCountObservable = subscriberCountSubject.asObservable();

  return { trackedObservable, subscriberCountObservable };
}
