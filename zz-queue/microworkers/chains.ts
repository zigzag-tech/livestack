import { InferPipeInputDef, InferPipeInputsDef, PipeDef } from "./PipeDef";
import { ZZPipe } from "./ZZPipe";
import { z } from "zod";

export async function addJobChain<
  UpstreamDef extends PipeDef<
    unknown,
    unknown,
    Record<string | number | symbol, unknown>,
    unknown,
    unknown
  >,
  DownstreamDef extends PipeDef<
    unknown,
    unknown,
    Record<string | number | symbol, unknown>,
    unknown,
    unknown
  >,
  UpstreamOutput = z.infer<UpstreamDef["outputDef"]>,
  DownstreamInputs = InferPipeInputsDef<DownstreamDef>,
  DownstreamInput = InferPipeInputDef<DownstreamDef>
>({
  upstream: { pipe: upstreamPipe, jobId: upstreamJobId },
  downstream: { inputKey, pipe: downstreamPipe, jobId: downstreamJobId },
}: {
  upstream: {
    pipe: ZZPipe<UpstreamDef>;
    jobId: string;
  };
  downstream: {
    inputKey?: keyof DownstreamInputs;
    pipe: ZZPipe<DownstreamDef>;
    jobId: string;
  };
}): Promise<void> {
  if (!inputKey) {
  }
}
