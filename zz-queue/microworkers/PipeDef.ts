import { z } from "zod";
import { ZZStream } from "./ZZStream";

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
  output: ZZStream<O> | z.ZodType<O>;
  inputs?: {
    [K in keyof StreamI]: ZZStream<StreamI[K]> | z.ZodType<StreamI[K]>;
  };
  input?: ZZStream<StreamI[keyof StreamI]> | z.ZodType<StreamI[keyof StreamI]>;
  progressDef?: z.ZodType<TProgress>;
}

export class PipeDef<
  P,
  O,
  StreamI extends Record<string | number | symbol, any>,
  TProgress
> {
  readonly name: string;
  readonly jobParamsDef: z.ZodType<P>;
  readonly output: ZZStream<O>;
  readonly inputs: {
    [K in keyof StreamI]: ZZStream<StreamI[K]>;
  };
  readonly progressDef: z.ZodType<TProgress>;

  constructor({
    name,
    jobParamsDef,
    output,
    inputs,
    input,
    progressDef,
  }: PipeDefParams<P, O, StreamI, TProgress>) {
    this.name = name;
    this.jobParamsDef = jobParamsDef;
    this.output = this.ensureStream("in", output);
    if (inputs) {
      this.inputs = Object.entries(inputs).map(([key, inp]) =>
        this.ensureStream("in", inp, key)
      ) as {
        [K in keyof StreamI]: ZZStream<StreamI[K]>;
      };
    } else if (input) {
      this.inputs = {
        default: this.ensureStream("in", input),
      } as {
        [K in keyof StreamI]: ZZStream<StreamI[K]>;
      };
    } else {
      throw new Error(
        `Either "input" (single stream) or "inputs" (multiple streams, as a key-value object) must be specified to define a pipe.`
      );
    }
    this.progressDef = progressDef || z.never();
  }

  private ensureStream<T>(
    type: "in" | "out",
    stream: ZZStream<T> | z.ZodType<T>,
    key?: string
  ): ZZStream<T> {
    if (stream instanceof ZZStream) {
      return stream;
    } else {
      let uniqueName = `${this.name}::${type}`;
      if (key !== undefined) {
        uniqueName += `/${key}`;
      }
      return ZZStream.get({
        uniqueName,
        def: stream,
      });
    }
  }

  // public derive<NewP, NewO, NewStreamI extends any[], NewWP, NewTP>(
  //   newP: Partial<PipeParams<NewP, NewO, NewStreamI, NewWP, NewTP>>
  // ) {
  //   return new PipeDef({
  //     ...this,
  //     ...newP,
  //   } as PipeParams<NewP extends {} ? NewP : P, NewO extends {} ? NewO : O, NewStreamI extends any[] ? NewStreamI : StreamI, NewWP extends {} ? NewWP : WP, NewTP extends {} ? NewTP : TProgress>);
  // }
}

const someJobParamsSchema = z.object({
  someField: z.string(),
});

const someOutputSchema = ZZStream.get({
  uniqueName: "someOutputSchema",
  def: z.object({
    someField: z.string(),
  }),
});

const schemaForStreamType1 = z.object({
  someField1: z.string(),
});

const schemaForStreamType2 = z.object({
  someField2: z.string(),
});

const myPipeDef = new PipeDef({
  name: "myPipe",
  jobParamsDef: someJobParamsSchema,
  output: someOutputSchema,
  inputs: {
    a: schemaForStreamType1,
    b: ZZStream.get({
      uniqueName: "someStream2",
      def: schemaForStreamType2,
    }),
  } as const,
  // other properties
});
