import { StreamDefSet, InferDefMap } from "./StreamDefSet";
import { ZodType, z } from "zod";

export abstract class IOSpec<I, O, IMap = InferTMap<I>, OMap = InferTMap<O>> {
  public readonly name: string;
  public readonly inputDefSet: StreamDefSet<IMap>;
  public readonly outputDefSet: StreamDefSet<OMap>;
  public readonly input: InferDefMap<IMap>;
  public readonly output: InferDefMap<OMap>;
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
    this.input = wrapIfSingle(input);
    this.output = wrapIfSingle(output);
    if (!input) {
      this.inputDefSet = new StreamDefSet({
        defs: {} as InferDefMap<IMap>,
      });
    } else {
      this.inputDefSet = new StreamDefSet({
        defs: this.input,
      });
    }
    if (!output) {
      this.outputDefSet = new StreamDefSet({
        defs: {} as InferDefMap<OMap>,
      });
    } else {
      this.outputDefSet = new StreamDefSet({
        defs: this.output,
      });
    }
  }
}
export type InferTMap<T> = T extends z.ZodType
  ? { [t in NoInfer<"default"> as string]: z.infer<T> }
  : T extends Record<string, ZodType>
  ? {
      [K in keyof T]: z.infer<T[K]>;
    }
  : never;
export function wrapIfSingle<
  T,
  TMap = T extends z.ZodType<infer TMap>
    ? {
        [t in NoInfer<"default"> as string]: z.ZodType<TMap>;
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
  } else if (def instanceof z.ZodType) {
    return {
      default: def!,
    } as TMap;
  } else {
    return def as unknown as TMap;
  }
}
