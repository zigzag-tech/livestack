// import { ZZJobSpec } from "./ZZJobSpec";
// import { z } from "zod";
// import { ZZStream, hashDef } from "./ZZStream";
// import { InferStreamSetType, UnknownTMap } from "./StreamDefSet";
// import { ZZEnv } from "./ZZEnv";

// const jobSpec1, jobSpec2;

// connectJobSpecs([jobSpec1, jobSpec2]);

// type JobSpecAndOutpet<
//   T extends ZZJobSpec<unknown, UnknownTMap, UnknownTMap, unknown>
// > = [ZZJobSpec<T>, keyof InferStreamSetType<T["inputDefSet"]>] | ZZJobSpec<T>;

// type JobSpecConnector<
//   T1 extends ZZJobSpec<unknown, UnknownTMap, UnknownTMap, unknown>,
//   T2 extends ZZJobSpec<unknown, UnknownTMap, UnknownTMap, unknown>
// > = {
//   in: {
//     jobSpec: T1;
//     key: keyof InferStreamSetType<T1["inputDefSet"]>;
//   };
//   out: {
//     jobSpec: T2;
//     key: keyof InferStreamSetType<T1["outputDefSet"]>;
//   };
// } | [
//   {
//     jobSpec: T1;
//     key: keyof InferStreamSetType<T1["inputDefSet"]>;
//   },
//   {
//     jobSpec: T2;
//     key: keyof InferStreamSetType<T1["outputDefSet"]>;
//   }
// ]
// ];

// function connectJobSpecs<
//   T1 extends ZZJobSpec<unknown, UnknownTMap, UnknownTMap, unknown>,
//   T2 extends ZZJobSpec<unknown, UnknownTMap, UnknownTMap, unknown>
// >(p1: JobSpecAndOutpet<T1>, p2: JobSpecAndOutpet<T2>): JobSpecConnector<T1, T2> {
//   // process p1: if array, get jobSpec and key; else, get jobSpec and key as "default"
//   const p1JobSpec = Array.isArray(p1) ? p1[0] : p1;
//   const p1Key = Array.isArray(p1) ? p1[1] : "default";
//   const p1StreamDef = p1JobSpec.outputDefSet.getDef(p1Key);

//   // process p2: if array, get jobSpec and key; else, get jobSpec and key as "default"
//   const p2JobSpec = Array.isArray(p2) ? p2[0] : p2;
//   const p2Key = Array.isArray(p2) ? p2[1] : "default";
//   const p2StreamDef = p2JobSpec.inputDefSet.getDef(p2Key);

//   // check if p1StreamDef and p2StreamDef are compatible
//   if (hashDef(p1StreamDef) !== hashDef(p2StreamDef)) {
//     throw new Error(
//       `Stream ${p1JobSpec.name}.${String(p1Key)} and ${p2JobSpec.name}.${String(
//         p2Key
//       )} are not compatible.`
//     );
//   }

//   return [
//     {
//       jobSpec: p1JobSpec,
//       key: p1Key,
//     },
//     {
//       jobSpec: p2JobSpec,
//       key: p2Key,
//     },
//   ];
// }
// export class ZZCompositeJobSpec<
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
//     jobSpecs,
//     connectors,
//   }: {
//     jobSpecs: {
//       [K in number]: ZZJobSpec<Ps[K], IMaps[K], OMaps[K], TProgresses[K]>;
//     };
//     connectors: JobSpecConnector<
//       ZZJobSpec<Ps[number], IMaps[number], OMaps[number], TProgresses[number]>,
//       ZZJobSpec<Ps[number], IMaps[number], OMaps[number], TProgresses[number]>>[]
//   }) {

//   }
// }

// const jobSpec1 = new ZZJobSpec({
//   name: "jobSpec1",
//   jobParamsDef: z.object({}),
//   input: ZZStream.single(z.object({})),
//   output: ZZStream.single(z.object({})),
//   zzEnv: ZZEnv.global(),
// });

// const jobSpec2 = new ZZJobSpec({
//   name: "jobSpec2",
//   jobParamsDef: z.object({}),
//   input: ZZStream.single(z.object({})),
//   output: ZZStream.single(z.object({})),
//   zzEnv: ZZEnv.global(),
// });
// const jobSpec3 = new ZZJobSpec({
//   name: "jobSpec3",
//   jobParamsDef: z.object({}),
//   input: ZZStream.single(z.object({})),
//   output: ZZStream.single(z.object({})),
//   zzEnv: ZZEnv.global(),
// });

// new ZZCompositeJobSpec({
//   jobSpecs: [jobSpec1, jobSpec2] as const,
//   connectors: [[{
//     jobSpec: jobSpec1,
//     key: "default"
//   }, {
//     jobSpec: jobSpec2,
//     key: "default"
//   }],[
//     {
//       jobSpec: jobSpec2,
//       key: "default"
//     },
//     {
//       jobSpec: jobSpec3,
//       key: "default"
//     }
//   ]],
// });
