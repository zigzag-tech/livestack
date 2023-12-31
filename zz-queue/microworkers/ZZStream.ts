import { ZodType, z } from "zod";

export type InferStreamDef<T> = T extends ZZStream<infer P> ? P : never;

export class ZZStream<T> {
  public readonly def: ZodType<T>;
  public readonly uniqueName: string;

  static globalRegistry: { [key: string]: ZZStream<any> } = {};

  public static get<T>({
    uniqueName,
    def,
  }: {
    uniqueName: string;
    def: ZodType<T>;
  }): ZZStream<T> {
    if (ZZStream.globalRegistry[uniqueName]) {
      const existing = ZZStream.globalRegistry[uniqueName];
      z.coerce;
      // check if types match
      if (existing.def !== def) {
        throw new Error(
          `ZZStream ${uniqueName} already exists with different type`
        );
      }
      return existing;
    } else {
      const stream = new ZZStream({ uniqueName, def });
      ZZStream.globalRegistry[uniqueName] = stream;
      return stream;
    }
  }

  private constructor({
    uniqueName,
    def,
  }: {
    uniqueName: string;
    def: ZodType<T>;
  }) {
    this.def = def;
    this.uniqueName = uniqueName;
  }
}
