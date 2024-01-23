import { InferDefMap } from "./StreamDefSet";
import { StreamDefSet } from "./StreamDefSet";
import { z } from "zod";

export abstract class ZZJobSpecBase<P, IMap = {}, OMap = {}> {
  public readonly name: string;
  protected readonly inputDefSet: StreamDefSet<IMap>;
  protected readonly outputDefSet: StreamDefSet<OMap>;
  protected readonly input: InferDefMap<IMap> | undefined;
  protected readonly output: InferDefMap<OMap> | undefined;

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
        ` ${type} is ambiguous for spec "${
          this.name
        }"; found more than two with tags [${keys.join(
          ", "
        )}]. \nPlease specify which one to use with "${type}(tagName)".`
      );
    } else {
      return defSet.keys[0] as T extends "input" ? keyof IMap : keyof OMap;
    }
  }

  constructor({
    name,
    input,
    output,
  }: {
    name: string;
    input?: InferDefMap<IMap>;
    output?: InferDefMap<OMap>;
  }) {
    this.name = name;
    this.input = input;
    this.output = output;
    if (!input) {
      this.inputDefSet = new StreamDefSet({
        defs: {} as InferDefMap<IMap>,
      });
    } else {
      this.inputDefSet = new StreamDefSet({
        defs: input,
      });
    }
    if (!output) {
      this.outputDefSet = new StreamDefSet({
        defs: {} as InferDefMap<OMap>,
      });
    } else {
      this.outputDefSet = new StreamDefSet({
        defs: output,
      });
    }
  }
}

export function single<T>(def: z.ZodType<T, any>) {
  return {
    default: def,
  };
}

export function multi<
  TMap extends {
    [key: string]: z.ZodType<any, any>;
  }
>(def: TMap) {
  return def;
}
