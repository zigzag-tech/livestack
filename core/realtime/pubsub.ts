import { BehaviorSubject, Observable, Subscriber, Subscription } from "rxjs";

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
