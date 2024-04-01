export const PRIMTIVE_KEY = "__primitive__";
export const ARRAY_KEY = "__array__";

export function handlePrimitiveOrArray<T>(d: T) {
  // stringify if jobData is primitive
  let jobDataT:
    | T
    | { [PRIMTIVE_KEY]: T }
    | {
        [ARRAY_KEY]: T;
      };
  if (typeof d !== "object" || d === null) {
    jobDataT = {
      [PRIMTIVE_KEY]: d,
    };
  } else if (Array.isArray(d)) {
    jobDataT = {
      [ARRAY_KEY]: d,
    };
  } else {
    jobDataT = d;
  }
  return jobDataT;
}
export function convertMaybePrimtiveOrArrayBack<T>(
  p:
    | T
    | {
        [PRIMTIVE_KEY]: T;
      }
    | {
        [ARRAY_KEY]: T;
      }
): T {
  if (typeof p === "object" && p !== null && PRIMTIVE_KEY in p) {
    return p[PRIMTIVE_KEY];
  } else if (typeof p === "object" && p !== null && ARRAY_KEY in p) {
    return p[ARRAY_KEY];
  } else {
    return p as T;
  }
}
