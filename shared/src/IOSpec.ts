import { StreamDefSet, InferDefMap, createStreamDefSet } from "./StreamDefSet";
import { ZodType, z } from "zod";

export abstract class IOSpec<I, O, IMap = InferTMap<I>, OMap = InferTMap<O>> {
  public readonly name: string;
  public readonly input: StreamDefSet<IMap>;
  public readonly output: StreamDefSet<OMap>;

  protected readonly __inputDef: InferDefMap<IMap>;
  protected readonly __outputDef: InferDefMap<OMap>;
  public abstract get inputTags(): (keyof IMap)[];
  public abstract get outputTags(): (keyof OMap)[];

  protected constructor({
    name,
    input,
    output,
  }: {
    name: string;
    input: I;
    output: O;
  }) {
    this.name = name;
    this.__inputDef = wrapIfSingle(input);
    this.__outputDef = wrapIfSingle(output);
    if (!input) {
      ({ streamDefSet: this.input } = createStreamDefSet({
        defs: {} as InferDefMap<IMap>,
      }));
    } else {
      ({ streamDefSet: this.input } = createStreamDefSet({
        defs: this.__inputDef,
      }));
    }
    if (!output) {
      ({ streamDefSet: this.output } = createStreamDefSet({
        defs: {} as InferDefMap<OMap>,
      }));
    } else {
      ({ streamDefSet: this.output } = createStreamDefSet({
        defs: this.__outputDef,
      }));
    }
  }
}
export type InferTMap<T> = T extends z.ZodType
  ? { default: z.infer<T> }
  : T extends Record<string, ZodType>
  ? {
      [K in keyof T]: z.infer<T[K]>;
    }
  : never;
export function wrapIfSingle<
  T,
  TMap = T extends z.ZodType<infer TMap>
    ? {
        default: z.ZodType<TMap>;
      }
    : T extends {
        [key: string]: z.ZodType<any>;
      }
    ? T
    : T extends undefined
    ? {}
    : never
>(def: T): TMap {
  if (!def) {
    return {} as TMap;
  } else if (def instanceof z.ZodType || (def as any)._def?.typeName) {
    return {
      default: def!,
    } as TMap;
  } else {
    return def as unknown as TMap;
  }
}
