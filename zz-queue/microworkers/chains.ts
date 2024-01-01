import { InferDutyInputDef, InferDutyInputsDef, DutyDef } from "./DutyDef";
import { ZZDuty } from "./ZZDuty";
import { z } from "zod";

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
