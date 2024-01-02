import { ZZDuty } from "./ZZDuty";
import { z } from "zod";
import { ZZStream, hashDef } from "./ZZStream";
import { InferStreamSetType, UnknownTMap } from "./StreamDefSet";
import { ZZEnv } from "./ZZEnv";

const pipe1, pipe2;

connectPipes([pipe1, pipe2]);

type PipeAndOutpet<
  T extends ZZDuty<unknown, UnknownTMap, UnknownTMap, unknown>
> = [ZZDuty<T>, keyof InferStreamSetType<T["inputDefSet"]>] | ZZDuty<T>;

type PipeConnector<
  T1 extends ZZDuty<unknown, UnknownTMap, UnknownTMap, unknown>,
  T2 extends ZZDuty<unknown, UnknownTMap, UnknownTMap, unknown>
> = {
  in: {
    duty: T1;
    key: keyof InferStreamSetType<T1["inputDefSet"]>;
  };
  out: {
    duty: T2;
    key: keyof InferStreamSetType<T1["outputDefSet"]>;
  };
} | [
  {
    duty: T1;
    key: keyof InferStreamSetType<T1["inputDefSet"]>;
  },
  {
    duty: T2;
    key: keyof InferStreamSetType<T1["outputDefSet"]>;
  }
]
];

function connectPipes<
  T1 extends ZZDuty<unknown, UnknownTMap, UnknownTMap, unknown>,
  T2 extends ZZDuty<unknown, UnknownTMap, UnknownTMap, unknown>
>(p1: PipeAndOutpet<T1>, p2: PipeAndOutpet<T2>): PipeConnector<T1, T2> {
  // process p1: if array, get duty and key; else, get duty and key as "default"
  const p1Duty = Array.isArray(p1) ? p1[0] : p1;
  const p1Key = Array.isArray(p1) ? p1[1] : "default";
  const p1StreamDef = p1Duty.outputDefSet.getDef(p1Key);

  // process p2: if array, get duty and key; else, get duty and key as "default"
  const p2Duty = Array.isArray(p2) ? p2[0] : p2;
  const p2Key = Array.isArray(p2) ? p2[1] : "default";
  const p2StreamDef = p2Duty.inputDefSet.getDef(p2Key);

  // check if p1StreamDef and p2StreamDef are compatible
  if (hashDef(p1StreamDef) !== hashDef(p2StreamDef)) {
    throw new Error(
      `Stream ${p1Duty.name}.${String(p1Key)} and ${p2Duty.name}.${String(
        p2Key
      )} are not compatible.`
    );
  }

  return [
    {
      duty: p1Duty,
      key: p1Key,
    },
    {
      duty: p2Duty,
      key: p2Key,
    },
  ];
}
export class ZZCompositeDuty<
  P,
  IMap extends UnknownTMap,
  OMap extends UnknownTMap,
  Ps extends {
    [K in number]: any;
  },
  IMaps extends {
    [K in number]: any;
  },
  OMaps extends {
    [K in number]: any;
  },
  TProgresses extends {
    [K in number]: any;
  }
> {
  constructor({
    duties,
    connectors,
  }: {
    duties: {
      [K in number]: ZZDuty<Ps[K], IMaps[K], OMaps[K], TProgresses[K]>;
    };
    connectors: PipeConnector<
      ZZDuty<Ps[number], IMaps[number], OMaps[number], TProgresses[number]>,
      ZZDuty<Ps[number], IMaps[number], OMaps[number], TProgresses[number]>>[]
  }) {

    
  }
}

const duty1 = new ZZDuty({
  name: "duty1",
  jobParamsDef: z.object({}),
  input: ZZStream.single(z.object({})),
  output: ZZStream.single(z.object({})),
  zzEnv: ZZEnv.global(),
});

const duty2 = new ZZDuty({
  name: "duty2",
  jobParamsDef: z.object({}),
  input: ZZStream.single(z.object({})),
  output: ZZStream.single(z.object({})),
  zzEnv: ZZEnv.global(),
});
const duty3 = new ZZDuty({
  name: "duty3",
  jobParamsDef: z.object({}),
  input: ZZStream.single(z.object({})),
  output: ZZStream.single(z.object({})),
  zzEnv: ZZEnv.global(),
});

new ZZCompositeDuty({
  duties: [duty1, duty2] as const,
  connectors: [[{
    duty: duty1,
    key: "default"
  }, {
    duty: duty2,
    key: "default"
  }],[
    {
      duty: duty2,
      key: "default"
    },
    {
      duty: duty3,
      key: "default"
    }
  ]],
});
