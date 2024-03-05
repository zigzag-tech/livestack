import { z } from "zod";
import { DataStreamSubscriber, WithTimestamp } from "../jobs/DataStream";
import { Observable } from "rxjs";
export interface ByTagOutput<T> {
  [Symbol.asyncIterator]: () => AsyncIterableIterator<{
    data: T;
    timestamp: number;
  }>;
  nextValue: () => Promise<WrapWithTimestamp<T> | null>;
  mostRecentValue: () => Promise<WrapWithTimestamp<T> | null>;
  valueObservable: Observable<{
    data: T;
    timestamp: number;
  } | null>;
  getStreamId: () => Promise<string>;
}

export interface ByTagInput<T> {
  feed: (data: T) => Promise<void>;
  terminate: () => Promise<void>;
  getStreamId: () => Promise<string>;
}

export type WrapTerminateFalse<T> = {
  data: T;
  terminate: false;
};
export type WrapTerminatorAndDataId<T> =
  | WrapTerminateFalse<T>
  | {
      terminate: true;
    };

export type WrapWithTimestamp<T> = {
  data: T;
  timestamp: number;
  chunkId: string;
};

export function wrapTerminatorAndDataId<T>(t: z.ZodType<T>) {
  return z.discriminatedUnion("terminate", [
    z.object({
      data: t,
      terminate: z.literal(false),
    }),
    z.object({
      terminate: z.literal(true),
    }),
  ]);
}

export function wrapStreamSubscriberWithTermination<T>(
  streamIdP: Promise<string>,
  subscriberP: Promise<DataStreamSubscriber<WrapTerminatorAndDataId<T>>>
): ByTagOutput<T> {
  const newValueObservable = new Observable<WrapWithTimestamp<T> | null>(
    (subscriber) => {
      subscriberP.then((zzSub) => {
        zzSub.valueObservable.subscribe({
          next: (v) => {
            if (v.terminate) {
              subscriber.unsubscribe();
              subscriber.complete();
            } else {
              subscriber.next({
                data: v.data,
                timestamp: v.timestamp,
                chunkId: v.chunkId,
              });
            }
          },
          error: (err) => subscriber.error(err),
          complete: () => subscriber.complete(),
        });
      });
    }
  );
  // const newValueObservable = subscriber.valueObservable.pipe(
  //   map((v) => {
  //     if (v.terminate) {
  //       subscriber.unsubscribe();
  //       return null;
  //     } else {
  //       return v.data;
  //     }
  //   })
  // );

  const newNextValue = async () => {
    const subscriber = await subscriberP;
    const nextValue = await subscriber.nextValue();
    if (nextValue.terminate) {
      subscriber.unsubscribe();
      return null;
    } else {
      return {
        data: nextValue.data,
        timestamp: nextValue.timestamp,
        chunkId: nextValue.chunkId,
      };
    }
  };

  const mostRecentValue = async () => {
    const subscriber = await subscriberP;
    const mostRecent = (await subscriber.stream.valueByReverseIndex(
      0
    )) as WithTimestamp<WrapTerminatorAndDataId<T>> | null;
    if (!mostRecent) {
      return null;
    } else {
      if (mostRecent.terminate) {
        // try get second most recent
        const secondMostRecent = (await subscriber.stream.valueByReverseIndex(
          1
        )) as WithTimestamp<WrapTerminateFalse<T>> | null;
        if (!secondMostRecent) {
          return null;
        } else {
          return {
            data: secondMostRecent.data,
            timestamp: secondMostRecent.timestamp,
            chunkId: secondMostRecent.chunkId,
          };
        }
      } else {
        return {
          data: mostRecent.data,
          timestamp: mostRecent.timestamp,
          chunkId: mostRecent.chunkId,
        };
      }
    }
  };

  const getStreamId = async () => {
    return await streamIdP;
  };

  return {
    getStreamId,
    valueObservable: newValueObservable,
    nextValue: newNextValue,
    mostRecentValue: mostRecentValue,
    async *[Symbol.asyncIterator]() {
      let nextValue = await newNextValue();
      while (nextValue !== null) {
        yield nextValue;
        nextValue = await newNextValue();
      }
    },
  };
}
