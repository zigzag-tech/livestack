import { z } from "zod";
export type WrapTerminatorAndDataId<T> = {
  __zz_datapoint_id__: string;
} & (
  | {
      data: T;
      __zz_datapoint_id__: string;
      terminate: false;
    }
  | {
      terminate: true;
    }
);

export function wrapTerminatorAndDataId<T>(t: z.ZodType<T>) {
  return z.union([
    z.object({
      data: t,
      __zz_datapoint_id__: z.string(),
      terminate: z.literal(false),
    }),
    z.object({
      terminate: z.literal(true),
    }),
  ]) as z.ZodType<WrapTerminatorAndDataId<T>>;
}
