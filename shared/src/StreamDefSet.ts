import { z } from "zod";

export type InferStreamSetType<T> = T extends StreamDefSet<infer TMap>
  ? TMap
  : {};

export type InferDefMap<TMap> = {
  [K in keyof TMap]: z.ZodType<TMap[K]>;
};

// export class StreamDefSet<TMap> {
//   public readonly defs: InferDefMap<TMap>;

//   constructor({ defs }: { defs: InferDefMap<TMap> }) {
//     this.defs = defs;
//   }

//   get tags() {
//     return Object.keys(this.defs) as (keyof TMap)[];
//   }

//   // get isSingle() {
//   //   return Object.keys(this.defs).length === 1;
//   // }

//   public hasDef = (key: string) => {
//     return key in this.defs;
//   };

//   public getDef = <K extends keyof TMap>(key: K) => {
//     if (!this.hasDef(key as string))
//       throw new Error(`No def for key ${String(key)}`);
//     const def = (this.defs as Record<keyof TMap, z.ZodType<TMap[keyof TMap]>>)[
//       key as keyof TMap
//     ] as z.ZodType<TMap[K]>;
//     return def;
//   };
// }

export function createStreamDefSet<TMap, ExtraFields>({
  defs,
  extraFields,
}: {
  defs: InferDefMap<TMap>;
  extraFields?: ExtraFields;
}): {
  streamDefSet: StreamDefSet<TMap, ExtraFields>;
  tags: (keyof TMap)[];
} {
  const tags = Object.keys(defs) as (keyof TMap)[];
  const streamDefSet = {} as StreamDefSet<TMap, ExtraFields>;
  for (const tag of tags) {
    streamDefSet[tag] = { tag, def: defs[tag], ...extraFields } as StreamDefSet<
      TMap,
      ExtraFields
    >[keyof TMap];
  }
  return { streamDefSet, tags };
}

export type StreamDefSet<TMap, ExtraFields = {}> = {
  [K in keyof TMap]: TaggedStreamDef<K, TMap[K], ExtraFields>;
};

export type TaggedStreamDef<K, T, ExtraFields> = {
  tag: K;
  def: z.ZodType<T>;
} & ExtraFields;