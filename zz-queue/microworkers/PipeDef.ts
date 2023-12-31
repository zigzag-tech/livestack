import { z } from "zod";

export type InferPipeDef<T extends PipeDef<any, any, any, any>> =
  T extends PipeDef<infer P, infer O, infer StreamI, infer TProgress>
    ? PipeDef<P, O, StreamI, TProgress>
    : never;

export type InferPipeInputsDef<T extends PipeDef<any, any, any, any>> =
  InferPipeDef<T> extends PipeDef<any, any, infer StreamI, any>
    ? StreamI extends Record<string | number | symbol, any>
      ? {
          [K in keyof StreamI]: StreamI[K];
        }
      : never
    : never;

export interface PipeDefParams<
  P,
  O,
  StreamI extends Record<string | number | symbol, any>,
  TProgress
> {
  name: string;
  jobParamsDef: z.ZodType<P>;
  outputDef: z.ZodType<O>;
  inputDefs?: {
    [K in keyof StreamI]: z.ZodType<StreamI[K]>;
  };
  inputDef?: z.ZodType<StreamI[keyof StreamI]>;
  progressDef?: z.ZodType<TProgress>;
}

export class PipeDef<
  P,
  O,
  StreamI extends Record<string | number | symbol, any> = never,
  TProgress = never
> {
  readonly name: string;
  readonly jobParamsDef: z.ZodType<P>;
  readonly inputDefs: {
    [K in keyof StreamI]: z.ZodType<StreamI[K]>;
  };
  readonly outputDef: z.ZodType<O>;

  // readonly output: ZZStream<O>;
  // readonly inputs: {
  //   [K in keyof StreamI]: ZZStream<StreamI[K]>;
  // };
  readonly progressDef: z.ZodType<TProgress>;

  constructor({
    name,
    jobParamsDef,
    outputDef,
    inputDefs,
    inputDef,
    progressDef,
  }: PipeDefParams<P, O, StreamI, TProgress>) {
    this.name = name;
    this.jobParamsDef = jobParamsDef;
    this.outputDef = outputDef;
    if (inputDefs) {
      this.inputDefs = inputDefs;
    } else if (inputDef) {
      this.inputDefs = {
        default: inputDef,
      } as {
        [K in keyof StreamI]: z.ZodType<StreamI[K]>;
      };
    } else {
      throw new Error(
        `Either "input" (single stream) or "inputs" (multiple streams, as a key-value object) must be specified to define a pipe.`
      );
    }
    this.progressDef = progressDef || z.never();
  }

  // private ensureStream<T>(
  //   type: "in" | "out",
  //   stream: ZZStream<T> | z.ZodType<T>,
  //   key?: string
  // ): ZZStream<T> {
  //   if (stream instanceof ZZStream) {
  //     return stream;
  //   } else {
  //     let uniqueName = `${this.name}::${type}`;
  //     if (key !== undefined) {
  //       uniqueName += `/${key}`;
  //     }
  //     return ZZStream.get({
  //       uniqueName,
  //       def: stream,
  //     });
  //   }
  // }

  // public derive<NewP, NewO, NewStreamI extends any[], NewWP, NewTP>(
  //   newP: Partial<PipeParams<NewP, NewO, NewStreamI, NewWP, NewTP>>
  // ) {
  //   return new PipeDef({
  //     ...this,
  //     ...newP,
  //   } as PipeParams<NewP extends {} ? NewP : P, NewO extends {} ? NewO : O, NewStreamI extends any[] ? NewStreamI : StreamI, NewWP extends {} ? NewWP : WP, NewTP extends {} ? NewTP : TProgress>);
  // }
}
