import { InferDefMap } from "./StreamDefSet";
import { StreamDefSet } from "./StreamDefSet";
import { ZodType, z } from "zod";

export abstract class IOSpec<I, O, IMap = InferTMap<I>, OMap = InferTMap<O>> {
  public readonly name: string;
  public readonly inputDefSet: StreamDefSet<IMap>;
  public readonly outputDefSet: StreamDefSet<OMap>;
  protected readonly input: InferDefMap<IMap>;
  protected readonly output: InferDefMap<OMap>;

  public getSingleInputTag() {
    return this.getSingleTag("input");
  }
  public getSingleOutputTag() {
    return this.getSingleTag("output");
  }
  private getSingleTag<T extends "input" | "output">(
    type: T
  ): T extends "input" ? keyof IMap : keyof OMap {
    const defSet = type === "input" ? this.inputDefSet : this.outputDefSet;
    if (defSet.keys.length === 0) {
      throw new Error(
        `No ${type} found for spec "${this.name}". Please specify at least one in the "${type}" field of the spec's definition.`
      );
    } else if (defSet.keys.length > 1) {
      const keys = defSet.keys;
      throw new Error(
        `Ambiguous ${type} for spec "${
          this.name
        }"; found more than two with tags [${keys.join(
          ", "
        )}]. \nPlease specify which one to use with "${type}(tagName)".`
      );
    } else {
      return defSet.keys[0] as T extends "input" ? keyof IMap : keyof OMap;
    }
  }

  constructor({ name, input, output }: { name: string; input: I; output: O }) {
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
  } else if (def instanceof z.ZodType) {
    return {
      default: def!,
    } as TMap;
  } else {
    return def as TMap;
  }
}
