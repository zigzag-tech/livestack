import { z } from "zod";
export type WrapTerminatorAndDataId<T> =
  | {
      data: T;
      terminate: false;
    }
  | {
      terminate: true;
    };

export function wrapTerminatorAndDataId<T>(t: z.ZodType<T>) {
  return z.union([
    z.object({
      data: t,
      terminate: z.literal(false),
    }),
    z.object({
      terminate: z.literal(true),
    }),
  ]) as z.ZodType<WrapTerminatorAndDataId<T>>;
}
