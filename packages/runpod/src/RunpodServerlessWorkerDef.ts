import { JobSpec } from "@livestack/core";
import { ZZEnv } from "@livestack/core";
import { ZZWorkerDef } from "@livestack/core";

export class RunpodServerlessWorkerDef<I extends object, O> extends ZZWorkerDef<
  any,
  any,
  any,
  any,
  { [k: string]: I },
  { [k: string]: O }
> {
  protected _endpointId: string;
  protected _runpodApiKey: string;

  constructor({
    serverlessEndpointId,
    runpodApiKey,
    jobSpec,
    zzEnv,
  }: {
    serverlessEndpointId: string;
    runpodApiKey: string;
    jobSpec: JobSpec<any, any, any, { [k: string]: I }, { [k: string]: O }>;
    zzEnv?: ZZEnv;
  }) {
    super({
      jobSpec,
      zzEnv,
      processor: async ({ input, logger }) => {
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

        let runpodResult = (await respP.json()) as RunpodResult;

        const statusUrl = `https://api.runpod.ai/v2/${this._endpointId}/status/${runpodResult.id}`;
        while (
          runpodResult.status === "IN_PROGRESS" ||
          runpodResult.status === "IN_QUEUE"
        ) {
          await sleep(1500);
          logger.info(`Checking status for job ${runpodResult.id}...`);
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
          logger.info(
            `Status for job ${runpodResult.id}: ${runpodResult.status}`
          );
        }

        if (runpodResult.status === "CANCELLED") {
          throw new Error(`Runpod job ${runpodResult.id} was cancelled.`);
        } else if (runpodResult.status === "FAILED") {
          console.error(runpodResult);
          throw new Error(`Runpod job ${runpodResult.id} failed.`);
        }

        // await update({
        //   incrementalData: {
        //     runpodResult: runpodResult.output,
        //     status: "FINISH",
        //   } as Partial<TJobData & { status: "FINISH"; runpodResult: TJobResult }>,
        // });

        return runpodResult.output;
      },
    });
    this._endpointId = serverlessEndpointId;
    this._runpodApiKey = runpodApiKey;
  }
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
