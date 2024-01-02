// import { ZZPipe } from "./ZZPipe";
// import { z } from "zod";
// import { ZZStream, hashDef } from "./ZZStream";
// import { InferStreamSetType, UnknownTMap } from "./StreamDefSet";
// import { ZZEnv } from "./ZZEnv";

// const pipe1, pipe2;

// connectPipes([pipe1, pipe2]);

// type PipeAndOutpet<
//   T extends ZZPipe<unknown, UnknownTMap, UnknownTMap, unknown>
// > = [ZZPipe<T>, keyof InferStreamSetType<T["inputDefSet"]>] | ZZPipe<T>;

// type PipeConnector<
//   T1 extends ZZPipe<unknown, UnknownTMap, UnknownTMap, unknown>,
//   T2 extends ZZPipe<unknown, UnknownTMap, UnknownTMap, unknown>
// > = {
//   in: {
//     pipe: T1;
//     key: keyof InferStreamSetType<T1["inputDefSet"]>;
//   };
//   out: {
//     pipe: T2;
//     key: keyof InferStreamSetType<T1["outputDefSet"]>;
//   };
// } | [
//   {
//     pipe: T1;
//     key: keyof InferStreamSetType<T1["inputDefSet"]>;
//   },
//   {
//     pipe: T2;
//     key: keyof InferStreamSetType<T1["outputDefSet"]>;
//   }
// ]
// ];

// function connectPipes<
//   T1 extends ZZPipe<unknown, UnknownTMap, UnknownTMap, unknown>,
//   T2 extends ZZPipe<unknown, UnknownTMap, UnknownTMap, unknown>
// >(p1: PipeAndOutpet<T1>, p2: PipeAndOutpet<T2>): PipeConnector<T1, T2> {
//   // process p1: if array, get pipe and key; else, get pipe and key as "default"
//   const p1Pipe = Array.isArray(p1) ? p1[0] : p1;
//   const p1Key = Array.isArray(p1) ? p1[1] : "default";
//   const p1StreamDef = p1Pipe.outputDefSet.getDef(p1Key);

//   // process p2: if array, get pipe and key; else, get pipe and key as "default"
//   const p2Pipe = Array.isArray(p2) ? p2[0] : p2;
//   const p2Key = Array.isArray(p2) ? p2[1] : "default";
//   const p2StreamDef = p2Pipe.inputDefSet.getDef(p2Key);

//   // check if p1StreamDef and p2StreamDef are compatible
//   if (hashDef(p1StreamDef) !== hashDef(p2StreamDef)) {
//     throw new Error(
//       `Stream ${p1Pipe.name}.${String(p1Key)} and ${p2Pipe.name}.${String(
//         p2Key
//       )} are not compatible.`
//     );
//   }

//   return [
//     {
//       pipe: p1Pipe,
//       key: p1Key,
//     },
//     {
//       pipe: p2Pipe,
//       key: p2Key,
//     },
//   ];
// }
// export class ZZCompositePipe<
//   P,
//   IMap extends UnknownTMap,
//   OMap extends UnknownTMap,
//   Ps extends {
//     [K in number]: any;
//   },
//   IMaps extends {
//     [K in number]: any;
//   },
//   OMaps extends {
//     [K in number]: any;
//   },
//   TProgresses extends {
//     [K in number]: any;
//   }
// > {
//   constructor({
//     pipes,
//     connectors,
//   }: {
//     pipes: {
//       [K in number]: ZZPipe<Ps[K], IMaps[K], OMaps[K], TProgresses[K]>;
//     };
//     connectors: PipeConnector<
//       ZZPipe<Ps[number], IMaps[number], OMaps[number], TProgresses[number]>,
//       ZZPipe<Ps[number], IMaps[number], OMaps[number], TProgresses[number]>>[]
//   }) {

//   }
// }

// const pipe1 = new ZZPipe({
//   name: "pipe1",
//   jobParamsDef: z.object({}),
//   input: ZZStream.single(z.object({})),
//   output: ZZStream.single(z.object({})),
//   zzEnv: ZZEnv.global(),
// });

// const pipe2 = new ZZPipe({
//   name: "pipe2",
//   jobParamsDef: z.object({}),
//   input: ZZStream.single(z.object({})),
//   output: ZZStream.single(z.object({})),
//   zzEnv: ZZEnv.global(),
// });
// const pipe3 = new ZZPipe({
//   name: "pipe3",
//   jobParamsDef: z.object({}),
//   input: ZZStream.single(z.object({})),
//   output: ZZStream.single(z.object({})),
//   zzEnv: ZZEnv.global(),
// });

// new ZZCompositePipe({
//   pipes: [pipe1, pipe2] as const,
//   connectors: [[{
//     pipe: pipe1,
//     key: "default"
//   }, {
//     pipe: pipe2,
//     key: "default"
//   }],[
//     {
//       pipe: pipe2,
//       key: "default"
//     },
//     {
//       pipe: pipe3,
//       key: "default"
//     }
//   ]],
// });
