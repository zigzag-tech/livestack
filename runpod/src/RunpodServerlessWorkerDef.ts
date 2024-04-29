import { JobSpec } from "@livestack/core";
import { ZZEnv } from "@livestack/core";
import { ZZWorkerDef } from "@livestack/core";
import { z } from "zod";

const StatusSchema = z.enum(["initiated", "waiting", "completed", "failed"]);
export class RunpodServerlessWorkerDef<I extends object, O> extends ZZWorkerDef<
  any,
  any,
  any,
  any,
  { default: I },
  { default: O | null; status: z.infer<typeof StatusSchema> }
> {
  protected _endpointId: string;
  protected _runpodApiKey: string;

  constructor({
    serverlessEndpointId,
    runpodApiKey,
    inputSchema,
    outputSchema,
    zzEnv,
    name,
    autostartWorker = true,
  }: {
    serverlessEndpointId: string;
    runpodApiKey: string;
    inputSchema: z.ZodType<I>;
    outputSchema: z.ZodType<O | null>;
    name: string;
    zzEnv?: ZZEnv;
    autostartWorker?: boolean;
  }) {
    const jobSpec = JobSpec.define({
      name: name,
      input: { default: inputSchema },
      output: {
        default: outputSchema,
        status: StatusSchema,
      },
      zzEnv,
    });

    super({
      jobSpec,
      zzEnv,
      autostartWorker,
      processor: async ({ input, output, logger }) => {
        await output("status").emit("initiated");
        try {
          const runUrl = `https://api.runpod.ai/v2/${this._endpointId}/runsync`;
          const data = (await input.nextValue())!;

          const respP = await fetch(runUrl, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${this._runpodApiKey}`,
            },
            body: JSON.stringify({
              input: data,
            }),
          });

          if (!respP.ok) {
            const errorBody = await respP.text();
            throw new Error(
              `Fetch to runpod serverless endpoint ${this._endpointId} failed with status ${respP.status}: ${errorBody}`
            );
          }
          type RunpodResult = { id: string; delayTime: number } & (
            | {
                status: "IN_PROGRESS";
              }
            | {
                status: "IN_QUEUE";
              }
            | {
                status: "CANCELLED";
              }
            | {
                status: "FAILED";
              }
            | {
                status: "COMPLETED";
                executionTime: number;
                output: O;
              }
          );
          logger.info(
            `Submitting to runpod serverless endpoint ${this._endpointId}...`
          );
          await output("status").emit("waiting");
          let runpodResult = (await respP.json()) as RunpodResult;
          logger.info(
            `Job submitted to to runpod serverless endpoint ${this._endpointId}.`
          );

          const statusUrl = `https://api.runpod.ai/v2/${this._endpointId}/status/${runpodResult.id}`;
          while (
            runpodResult.status === "IN_PROGRESS" ||
            runpodResult.status === "IN_QUEUE"
          ) {
            await output("status").emit("waiting");

            await sleep(1500);
            // logger.info(`Checking status for job ${runpodResult.id}...`);
            const respP = await fetch(statusUrl, {
              method: "GET",
              headers: {
                Authorization: `Bearer ${this._runpodApiKey}`,
              },
            });

            if (!respP.ok) {
              const errorBody = await respP.text();
              throw new Error(
                `Fetch to runpod serverless endpoint ${this._endpointId} failed with status ${respP.status}: ${errorBody}`
              );
            }

            runpodResult = await respP.json();
            // logger.info(
            //   `Status for job ${runpodResult.id}: ${runpodResult.status}.`
            // );
          }

          if (runpodResult.status === "CANCELLED") {
            await output("status").emit("failed");
            throw new Error(`Runpod job ${runpodResult.id} was cancelled.`);
          } else if (runpodResult.status === "FAILED") {
            await output("status").emit("failed");
            console.error(runpodResult);
            throw new Error(`Runpod job ${runpodResult.id} failed.`);
          }

          if (runpodResult.status !== "COMPLETED") {
            throw new Error(
              `Unexpected status: ${(runpodResult as any).status}`
            );
          }

          logger.info(
            `Result obtained from runpod serverless endpoint ${
              this._endpointId
            }. Bytes received: ${JSON.stringify(runpodResult.output).length}.
            `
          );
          // await update({
          //   incrementalData: {
          //     runpodResult: runpodResult.output,
          //     status: "FINISH",
          //   } as Partial<TJobData & { status: "FINISH"; runpodResult: TJobResult }>,
          // });

          await output.emit(runpodResult.output);
          await output("status").emit("completed");
        } catch (e) {
          console.error(e);
          await output.emit(null);
          await output("status").emit("failed");
          throw e;
        }
      },
    });
    this._endpointId = serverlessEndpointId;
    this._runpodApiKey = runpodApiKey;
  }
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
