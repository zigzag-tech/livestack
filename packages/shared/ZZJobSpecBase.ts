import { InferDefMap } from "./StreamDefSet";
import { StreamDefSet } from "./StreamDefSet";
import { z } from "zod";

export abstract class ZZJobSpecBase<P, IMap = {}, OMap = {}> {
  public readonly name: string;
  public readonly inputDefSet: StreamDefSet<IMap>;
  public readonly outputDefSet: StreamDefSet<OMap>;
  public readonly input: InferDefMap<IMap> | undefined;
  public readonly output: InferDefMap<OMap> | undefined;

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
