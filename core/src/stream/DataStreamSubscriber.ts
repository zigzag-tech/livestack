import { LiveEnv } from "../env/LiveEnv";
import {
  identifyLargeFilesToRestore,
  restoreLargeValues,
} from "../files/file-ops";
import { SubType } from "@livestack/vault-interface/src/generated/stream";
import { Observable, Subscriber } from "rxjs";
import { createLazyNextValueGenerator } from "../jobs/pubsub";
import { DataStream, WithTimestamp } from "./DataStream";

export class DataStreamSubscriber<T extends object> {
  private liveEnvP: Promise<LiveEnv>;
  public readonly stream: DataStream<T>;
  private cursor: `${string}-${string}` | "$" | "0";
  private _valueObservable: Observable<WithTimestamp<T>> | null = null;
  private isUnsubscribed: boolean = false;
  private _nextValue: (() => Promise<WithTimestamp<T>>) | null = null;
  private subType: SubType;

  constructor({
    stream,
    liveEnvP,
    initialCursor,
    subType,
  }: {
    stream: DataStream<T>;
    liveEnvP: Promise<LiveEnv>;
    initialCursor: "0" | "$";
    subType: SubType;
  }) {
    this.stream = stream;
    this.liveEnvP = liveEnvP;
    this.cursor = initialCursor;
    this.subType = subType;
  }

  static subFromNow<T extends object>(stream: DataStream<T>) {
    return new DataStreamSubscriber({
      stream,
      liveEnvP: stream.liveEnvP,
      initialCursor: "$",
      subType: SubType.fromNow,
    });
  }

  static subFromBeginning<T extends object>(stream: DataStream<T>) {
    return new DataStreamSubscriber({
      stream,
      liveEnvP: stream.liveEnvP,
      initialCursor: "0",
      subType: SubType.fromStart,
    });
  }

  private initializeObservable() {
    this._valueObservable = new Observable(
      (subscriber: Subscriber<WithTimestamp<T>>) => {
        this.readStream(subscriber);
      }
    );
  }

  private async readStream(subscriber: Subscriber<WithTimestamp<T>>) {
    try {
      const iter = (await LiveEnv.globalP()).vaultClient.stream.sub({
        projectUuid: (await this.liveEnvP).projectUuid,
        uniqueName: this.stream.uniqueName,
        subType: this.subType,
      });
      // console.debug("DataStreamSubscriber readStream", this.stream.uniqueName);
      for await (const message of iter) {
        // console.debug(
        //   "DataStreamSubscriber message",
        //   this.stream.uniqueName,
        //   message
        // );
        const data: T = JSON.parse(message.dataStr);
        let restored = data;
        const { largeFilesToRestore, newObj } =
          identifyLargeFilesToRestore(data);

        if (largeFilesToRestore.length > 0) {
          const liveEnv = await this.liveEnvP;
          const storageProvider = await liveEnv.storageProvider;
          if (!storageProvider) {
            throw new Error(
              "storageProvider is not provided, and not all parts can be saved to local storage because they are either too large or contains binary data."
            );
          } else {
            restored = (await restoreLargeValues({
              obj_: newObj,
              largeFilesToRestore,
              basePath: await this.stream.baseWorkingRelativePathP,
              fetcher: storageProvider.fetchFromStorage,
            })) as T;
          }
        }

        subscriber.next({
          ...restored,
          timestamp: message.timestamp,
          chunkId: message.chunkId,
          datapointId: message.datapointId,
        });
      }
    } catch (error) {
      subscriber.error(error);
    } finally {
      // Perform cleanup here if necessary
      subscriber.complete();
    }
  }

  public get valueObservable(): Observable<WithTimestamp<T>> {
    if (!this._valueObservable) {
      this.initializeObservable();
    }
    return this._valueObservable!;
  }

  public unsubscribe = () => {
    this.isUnsubscribed = true;
    // Perform any additional cleanup or resource release here if necessary
  };

  public nextValue = () => {
    if (!this._nextValue) {
      const { nextValue } = createLazyNextValueGenerator(this.valueObservable);
      this._nextValue = () => nextValue();
    }
    return this._nextValue();
  };

  async *[Symbol.asyncIterator]() {
    while (true) {
      const input = await this.nextValue();

      // Assuming nextInput returns null or a similar value to indicate completion
      if (!input) {
        break;
      }
      yield input;
    }
  }
}
