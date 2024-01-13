import { z } from "zod";
import { ZZStreamSubscriber } from "../orchestrations/ZZStream";
import { Observable } from "rxjs";
export type WrapTerminatorAndDataId<T> =
  | {
      data: T;
      terminate: false;
    }
  | {
      terminate: true;
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
  subscriberP: Promise<ZZStreamSubscriber<WrapTerminatorAndDataId<T>>>
) {
  const newValueObservable = new Observable<T | null>((subscriber) => {
    subscriberP.then((zzSub) => {
      zzSub.valueObservable.subscribe({
        next: (v) => {
          if (v.terminate) {
            subscriber.unsubscribe();
            subscriber.complete();
          } else {
            subscriber.next(v.data);
          }
        },
        error: (err) => subscriber.error(err),
        complete: () => subscriber.complete(),
      });
    });
  });
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
      return nextValue.data;
    }
  };

  const waitUntilTerminated = () => {
    return new Promise<void>((resolve, reject) => {
      newValueObservable.subscribe({
        next: (v) => {
          if (v === null) {
            resolve();
          }
        },
        error: reject,
      });
    });
  };

  return {
    valueObservable: newValueObservable,
    nextValue: newNextValue,
    unsubscribe: () => subscriberP.then((s) => s.unsubscribe()),
    waitUntilTerminated,
  };
}
