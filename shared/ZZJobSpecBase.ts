import { InferDefMap } from "./StreamDefSet";
import { z } from "zod";
import { StreamDefSet } from "./StreamDefSet";

export abstract class ZZJobSpecBase<
  P,
  IMap = {
    default: {};
  },
  OMap = {
    default: {};
  }
> {
  public readonly name: string;
  public readonly inputDefSet: StreamDefSet<IMap>;
  public readonly outputDefSet: StreamDefSet<OMap>;
  public readonly input: InferDefMap<IMap> | undefined;
  public readonly output: InferDefMap<OMap> | undefined;
  protected static _registryBySpecName: Record<
    string,
    ZZJobSpecBase<any, any, any>
  > = {};

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
        defs: single(z.object({})) as InferDefMap<IMap>,
      });
    } else {
      this.inputDefSet = new StreamDefSet({
        defs: input,
      });
    }
    if (!output) {
      this.outputDefSet = new StreamDefSet({
        defs: single(z.void()) as InferDefMap<OMap>,
      });
    } else {
      this.outputDefSet = new StreamDefSet({
        defs: output,
      });
    }

    ZZJobSpecBase._registryBySpecName[this.name] = this;
  }

  public static lookupByName(specName: string) {
    if (!ZZJobSpecBase._registryBySpecName[specName]) {
      throw new Error(`JobSpec ${specName} not defined on this machine.`);
    }
    return ZZJobSpecBase._registryBySpecName[specName];
  }
}

export function single<T>(def: z.ZodType<T, any>) {
  return {
    default: def,
  };
}
