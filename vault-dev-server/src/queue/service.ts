import {
  QueueServiceImplementation,
  QueueJob,
  FromWorker,
  ToWorker,
} from "@livestack/vault-interface";
import { InitInstanceParams } from "@livestack/vault-interface/src/generated/queue";
import { Queue, Worker } from "bullmq";
import { genPromiseCycle, genManuallyFedIterator } from "@livestack/shared";
import { CallContext } from "nice-grpc";
import { v4 } from "uuid";
import { createClient } from "redis";
import { escapeColon, getCapacityManager } from "../capacity/manager";

const _rawQueueBySpecName = new Map<string, Queue>();

class QueueServiceByProject implements QueueServiceImplementation {
  //   projectId: string;
  authMiddleware?: (context: CallContext) => Promise<void>;
  onJobAssignedToWorker?: (job: QueueJob, ctx: CallContext) => Promise<void>;
  constructor(p?: {
    authMiddleware?: (context: CallContext) => Promise<void>;
    onJobAssignedToWorker?: (job: QueueJob, ctx: CallContext) => Promise<void>;
  }) {
    const { authMiddleware } = p || {};
    // this.projectId = projectId;
    this.authMiddleware = authMiddleware;
    this.onJobAssignedToWorker = p?.onJobAssignedToWorker;
  }
  private redisClientP = createClient().connect();

  async initInstance(
    request: InitInstanceParams,
    context: CallContext
  ): Promise<{ instanceId?: string | undefined }> {
    const instanceId = v4();
    return { instanceId };
  }

  async addJob(job: QueueJob, context: CallContext) {
    // if (job.projectId !== this.projectId) {
    //   throw new Error("Invalid projectId " + job.projectId);
    // }

    if (this.authMiddleware) {
      await this.authMiddleware(context);
    }

    const queue = this.getQueue(job);
    // console.debug("addjob", queue.name, job);

    // get current capacity
    const client = await this.redisClientP;
    const projectIdN = escapeColon(job.projectId);
    const specNameN = escapeColon(job.specName);

    // const workers = await queue.getWorkers();

    // if (workers.length === 0) {
    //   // TODO: use logger
    //   console.warn(
    //     `No worker for queue ${queue.name}; job ${job.jobId} might be be stuck.`
    //   );
    // }

    await queue.add(
      job.jobId,
      {
        jobOptions: JSON.parse(job.jobOptionsStr),
        contextId:
          !job.contextId || job.contextId === "" ? null : job.contextId,
      },
      {
        // force job id to be the same as name
        jobId: job.jobId,
        removeOnComplete: true,
        removeOnFail: true,
      }
    );
    const c = (await queue.getWaitingCount()) + (await queue.getActiveCount());

    // const capacity =
    //   Number(
    //     await client.sendCommand([
    //       "HGET",
    //       `zz_capacity`,
    //       `${projectIdN}:${specNameN}`,
    //     ])
    //   ) || 0;

    const workers = await queue.getWorkers();
    const capacity = workers.length;

    if (c > 0 && c > capacity) {
      const diff = c - capacity;
      console.debug(
        `Queue ${job.specName} for project ${job.projectId} has ${c} waiting+active jobs, while capacity is at ${capacity}. \nAttempting to increase capacity by ${diff}.`
      );
      for (let i = 0; i < diff; i++) {
        await getCapacityManager().increaseCapacity({
          projectId: job.projectId,
          specName: job.specName,
          by: 1,
        });
      }
    }

    return {};
  }

  private getQueue({
    specName,
    projectId,
  }: {
    specName: string;
    projectId: string;
  }) {
    const queueId = `${projectId}/${specName}`;
    if (!_rawQueueBySpecName.has(queueId)) {
      _rawQueueBySpecName.set(
        queueId,
        new Queue(queueId, {
          connection: {
            host: process.env.REDIS_HOST || "localhost",
            port: Number(process.env.REDIS_PORT) || 6379,
          },
        })
      );
    }
    return _rawQueueBySpecName.get(queueId)!;
  }

  private workerBundleById: Record<
    string,
    {
      worker: Worker;
      updateProgress: null | ((progress: number) => Promise<void>);
      jobCompleteCycleByJobId: Record<
        string,
        ReturnType<typeof genPromiseCycle>
      >;
    }
  > = {};

  private currentJobByWorkerId: Record<string, QueueJob> = {};

  reportAsWorker(request: AsyncIterable<FromWorker>, context: CallContext) {
    const { iterator: iter, resolveNext: resolveJobPromise } =
      genManuallyFedIterator<ToWorker>();

    const sendJob = async ({
      job,
      workerId,
    }: {
      job: QueueJob;
      workerId: string;
    }) => {
      // console.debug("sendJob", workerId, job);
      this.currentJobByWorkerId[workerId] = job;
      if (this.onJobAssignedToWorker) {
        await this.onJobAssignedToWorker(job, context);
      }
      resolveJobPromise({ job, workerId }); // Resolve the current job promise with the job data
    };

    let updateProgress: null | ((progress: number) => Promise<void>) = null;

    (async () => {
      if (this.authMiddleware) {
        await this.authMiddleware(context);
      }
      for await (const {
        workerId,
        signUp,
        progressUpdate,
        jobCompleted,
        jobFailed,
        workerStopped,
      } of request) {
        if (signUp) {
          const { projectId, specName } = signUp;
          //   if (projectId !== this.projectId) {
          //     throw new Error("Invalid projectId " + projectId);
          //   }
          // console.debug("Adding worker", `${projectId}/${specName}`);
          const worker = new Worker(
            `${projectId}/${specName}`,
            async (job) => {
              const jobCompleteCycle = genPromiseCycle();
              this.workerBundleById[workerId].jobCompleteCycleByJobId[job.id!] =
                jobCompleteCycle;
              await sendJob({
                workerId,
                job: {
                  projectId: projectId,
                  jobId: job.id!,
                  specName,
                  jobOptionsStr: JSON.stringify(job.data.jobOptions),
                  contextId: job.data.contextId || undefined,
                },
              });

              updateProgress = job.updateProgress.bind(job);
              return await jobCompleteCycle.promise;
            },
            {
              concurrency: 1,
              connection: {
                host: process.env.REDIS_HOST || "localhost",
                port: Number(process.env.REDIS_PORT) || 6379,
              },
            }
          );

          this.workerBundleById[workerId] = {
            worker,
            updateProgress,
            jobCompleteCycleByJobId: {},
          };

          const client = await this.redisClientP;
          // increment the capacity for this worker by 1
          await client.sendCommand([
            "HINCRBY",
            `zz_capacity`,
            `${projectId}:${specName}`,
            "1",
          ]);

          await worker.waitUntilReady();
          const abortListener = async () => {
            // decrement the capacity for this worker by 1
            await client.sendCommand([
              "HINCRBY",
              `zz_capacity`,
              `${projectId}:${specName}`,
              "-1",
            ]);

            console.debug(
              "Worker stopped for",
              specName,
              ", id:",
              workerId,
              ". capacity for ",
              worker.name,
              " reduced to ",
              Number(
                await client.sendCommand([
                  "HGET",
                  `zz_capacity`,
                  `${projectId}:${specName}`,
                ])
              ) || 0
            );

            this.currentJobByWorkerId[workerId] &&
              this.workerBundleById[workerId]?.jobCompleteCycleByJobId[
                this.currentJobByWorkerId[workerId].jobId
              ].rejectNext({
                message: "Worker disconnected from vault server.",
              });

            await worker.close();
            delete this.workerBundleById[workerId];
            delete this.currentJobByWorkerId[workerId];
            
            context.signal.removeEventListener("abort", abortListener);
          };
          context.signal.addEventListener("abort", abortListener);

          // TODO
        } else if (progressUpdate) {
          if (!updateProgress) {
            console.error(progressUpdate);
            throw new Error("updateProgress not initialized");
          } else {
            // TODO: fix typing
            (updateProgress as any)(progressUpdate.progress);
          }
        } else if (jobCompleted) {
          if (!this.workerBundleById[workerId]) {
            throw new Error("resolveWorkerCompletePromise not initialized");
          }
          // console.debug("jobCompleted", jobCompleted);
          this.workerBundleById[workerId].jobCompleteCycleByJobId[
            jobCompleted.jobId
          ].resolveNext({});
        } else if (jobFailed) {
          if (!this.workerBundleById[workerId]) {
            throw new Error("rejectWorkerCompletePromise not initialized");
          }
          this.workerBundleById[workerId].jobCompleteCycleByJobId[
            jobFailed.jobId
          ].rejectNext({
            message: jobFailed.errorStr,
          });
        } else if (workerStopped) {
          if (!workerId) {
            throw new Error("workerId not initialized");
          }
          delete this.workerBundleById[workerId];
          return;
        } else {
          throw new Error("Unknown message from worker");
        }
      }
    })();

    return iter;
  }
}

const _projectServiceMap: Record<string, QueueServiceByProject> = {};

export const getQueueService = (p?: {
  authMiddleware?: (context: CallContext) => Promise<void>;
  onJobAssignedToWorker?: (job: QueueJob, ctx: CallContext) => Promise<void>;
}) => {
  if (!_projectServiceMap["default"]) {
    _projectServiceMap["default"] = new QueueServiceByProject(p);
  }
  return _projectServiceMap["default"];
};
