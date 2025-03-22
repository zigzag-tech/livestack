import { z } from "zod";
import { WithTimestamp } from "../stream/DataStream";
import { DataStreamSubscriber } from "../stream/DataStreamSubscriber";
import { Observable } from "rxjs";
export interface ByTagOutput<T> {
  [Symbol.asyncIterator]: () => AsyncIterableIterator<{
    data: T;
    timestamp: number;
  }>;
  nextValue: () => Promise<WrapWithTimestamp<T> | null>;
  mostRecent: (n?: number) => Promise<WrapWithTimestamp<T>[]>;
  allValues: () => Promise<(WrapWithTimestamp<T> | null)[]>;
  valueObservable: Observable<WrapWithTimestamp<T> | null>;
  getStreamId: () => Promise<string>;
}

export interface ByTagInput<T> {
  feed: <TT extends T>(data: TT) => Promise<void>;
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

  const mostRecent = async (n: number = 1) => {
    const subscriber = await subscriberP;
    let results = (await subscriber.stream.valuesByReverseIndex(
      n + 1
    )) as (WithTimestamp<WrapTerminatorAndDataId<T>> | null)[];

    if (!results[results.length - 1]) {
      return [];
    } else {
      if (results[results.length - 1]!.terminate) {
        // skip last one
        return results.slice(0, results.length - 1).map((v) => {
          return {
            data: (v as WithTimestamp<WrapTerminateFalse<T>>).data,
            timestamp: v!.timestamp,
            chunkId: v!.chunkId,
          };
        });
      } else {
        // skip first one
        return results.slice(1).map((v) => {
          return {
            data: (v as WithTimestamp<WrapTerminateFalse<T>>)!.data,
            timestamp: v!.timestamp,
            chunkId: v!.chunkId,
          };
        });
      }
    }
  };

  const allValues = async () => {
    const subscriber = await subscriberP;
    let results = await subscriber.stream.allValues();
    return results.map((v) => {
      return {
        data: (v as WithTimestamp<WrapTerminateFalse<T>>).data,
        timestamp: v!.timestamp,
        chunkId: v!.chunkId,
      };
    });
  };

  const getStreamId = async () => {
    return await streamIdP;
  };

  return {
    getStreamId,
    valueObservable: newValueObservable,
    nextValue: newNextValue,
    mostRecent: mostRecent,
    allValues: allValues,
    async *[Symbol.asyncIterator]() {
      let nextValue = await newNextValue();
      while (nextValue !== null) {
        yield nextValue;
        nextValue = await newNextValue();
      }
    },
  };
}
