export function genPromiseCycle<T>() {
  let resolve: null | ((value: T | PromiseLike<T>) => void);
  let reject: null | ((reason?: any) => void);
  const promiseQueue: (
    | { type: "resolve"; value: T }
    | { type: "reject"; reason: any }
  )[] = [];

  return {
    get promise() {
      if (promiseQueue.length > 0) {
        const next = promiseQueue.shift()!;
        if (next.type === "resolve") {
          return Promise.resolve(next.value);
        } else {
          return Promise.reject(next.reason);
        }
      } else {
        return new Promise<T>((res, rej) => {
          resolve = res;
          reject = rej;
        });
      }
    },
    resolveNext(value: T) {
      if (resolve) {
        resolve(value);
        resolve = null;
        reject = null;
      } else {
        promiseQueue.push({
          type: "resolve",
          value,
        });
      }
    },
    rejectNext(reason?: any) {
      if (reject) {
        reject(reason);
        resolve = null;
        reject = null;
      } else {
        promiseQueue.push({
          type: "reject",
          reason,
        });
      }
    },
  };
}

export function genManuallyFedIterator<T>(onNext?: (v: T) => void) {
  const g = genPromiseCycle<
    | {
        terminate: false;
        data: T;
      }
    | {
        terminate: true;
      }
  >();
  const terminate = () => {
    g.resolveNext({
      terminate: true,
    });
  };
  const iter = {
    async *[Symbol.asyncIterator]() {
      while (true) {
        const d = await g.promise;
        if (d.terminate) {
          break;
        }
        if (onNext) {
          onNext(d.data);
        }
        yield d.data;
      }
    },
  };
  return {
    iterator: iter,
    resolveNext: (v: T) =>
      g.resolveNext({
        terminate: false,
        data: v,
      }),
    rejectNext: (reason?: any) => g.rejectNext(reason),
    terminate,
  };
}
