export function wrapNullResponse<T, R>(
  f: (a: T) => Promise<{
    rec?: R;
    null_response?: {};
  }>
): (a: T) => Promise<R | null> {
  return async (a) => {
    const r = await f(a);
    if (r.null_response) {
      return null;
    }
    return r.rec as R;
  };
}

export type WrapServiceNullResponses<S> = {
  [K in keyof S]: S[K] extends (...args: infer T) => Promise<{
    rec?: infer R;
    null_response?: {};
  }>
    ? (...args: T) => Promise<R | null>
    : never;
};
