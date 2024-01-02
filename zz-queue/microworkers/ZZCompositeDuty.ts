import { InferDutyInputDef, InferDutyInputsDef, DutyDef } from "./DutyDef";
import { ZZDuty } from "./ZZDuty";
import { z } from "zod";

const pipe1, pipe2;

connectPipes([pipe1, pipe2]);

type PipeAndOutpet<
  T extends DutyDef<
    unknown,
    unknown,
    Record<string | number | symbol, unknown>,
    unknown,
    unknown
  >
> = [ZZDuty<T>, keyof InferDutyInputsDef<ZZDuty<T>>] | ZZDuty<T>;

type PipeConnector<
  T1 extends DutyDef<
    unknown,
    unknown,
    Record<string | number | symbol, unknown>,
    unknown,
    unknown
  >,
  T2 extends DutyDef<
    unknown,
    unknown,
    Record<string | number | symbol, unknown>,
    unknown,
    unknown
  >
> = {
  in: {
    duty: ZZDuty<T1>;
    key: keyof InferDutyOutputDef<ZZDuty<T1>>;
  };
  out: {
    duty: ZZDuty<T2>;
    key: keyof InferDutyInputsDef<ZZDuty<T2>>;
  };
};

function connectPipes<
  T1 extends DutyDef<
    unknown,
    unknown,
    Record<string | number | symbol, unknown>,
    unknown,
    unknown
  >,
  T2 extends DutyDef<
    unknown,
    unknown,
    Record<string | number | symbol, unknown>,
    unknown,
    unknown
  >
>([p1, p2]: [PipeAndOutpet<T1>, PipeAndOutpet<T2>]): PipeConnector<T1, T2> {
  // process p1: if array, get duty and key; else, get duty and key as "default"
  const p1Duty = Array.isArray(p1) ? p1[0] : p1;
  const p1Key = Array.isArray(p1) ? p1[1] : "default";
  let p1StreamDef: ZZDuty<T1>["outputDefs"][keyof ZZDuty<T1>["outputDefs"]];
  if (p1Duty.outputDefs.isSingle && p1Key !== "default") {
    throw new Error(
      `Duty ${p1Duty.name} has only one output, but you specified key ${p1Key}`
    );
  }
  const p1StreamDef = p1Duty.outputDefs;
  // process p2: if array, get duty and key; else, get duty and key as "default"
  const p2Duty = Array.isArray(p2) ? p2[0] : p2;
  const p2Key = Array.isArray(p2) ? p2[1] : "default";
  const p2StreamDef = p2Duty.inputDefs[p2Key];

  // check if p1StreamDef and p2StreamDef are compatible
  if (p1StreamDef.hash !== p2StreamDef.hash) {
    throw new Error(
      `Stream ${p1Duty.name}.${p1Key} and ${p2Duty.name}.${p2Key} are not compatible.`
    );
  }

  return {
    in: {
      duty: p1Duty,
      key: p1Key,
    },
    out: {
      duty: p2Duty,
      key: p2Key,
    },
  };
}

export async function addJobChain<
  UpstreamDef extends DutyDef<
    unknown,
    unknown,
    Record<string | number | symbol, unknown>,
    unknown,
    unknown
  >,
  DownstreamDef extends DutyDef<
    unknown,
    unknown,
    Record<string | number | symbol, unknown>,
    unknown,
    unknown
  >,
  UpstreamOutput = z.infer<UpstreamDef["outputDef"]>,
  DownstreamInputs = InferDutyInputsDef<DownstreamDef>,
  DownstreamInput = InferDutyInputDef<DownstreamDef>
>({
  upstream: { duty: upstreamDuty, jobId: upstreamJobId },
  downstream: { inputKey, duty: downstreamDuty, jobId: downstreamJobId },
}: {
  upstream: {
    duty: ZZDuty<UpstreamDef>;
    jobId: string;
  };
  downstream: {
    inputKey?: keyof DownstreamInputs;
    duty: ZZDuty<DownstreamDef>;
    jobId: string;
  };
}): Promise<void> {
  if (!inputKey) {
  }
}
