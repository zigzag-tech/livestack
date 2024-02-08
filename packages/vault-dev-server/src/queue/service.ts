import {
  QueueServiceImplementation,
  QueueJob,
} from "@livestack/vault-interface";
import {
  FromWorker,
  ServerStreamingMethodResult,
  ToWorker,
} from "@livestack/vault-interface/src/generated/queue";
import { Queue, Worker } from "bullmq";
import { genPromiseCycle, genManuallyFedIterator } from "@livestack/shared";

const _rawQueueBySpecName = new Map<string, Queue>();

class ProjectQueueService implements QueueServiceImplementation {
  //   projectId: string;
  //   constructor({ projectId }: { projectId: string }) {
  //     this.projectId = projectId;
  //   }

  async addJob(job: QueueJob) {
    // if (job.projectId !== this.projectId) {
    //   throw new Error("Invalid projectId " + job.projectId);
    // }
    const queue = this.getQueue(job);
    const workers = await queue.getWorkers();
    if (workers.length === 0) {
      // TODO: use logger
      console.warn(
        `No worker for queue ${queue.name}; job ${job.jobId} might be be stuck.`
      );
    }
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
      }
    );
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

  reportAsWorker(request: AsyncIterable<FromWorker>) {
    const { iterator: iter, resolveNext: resolveJobPromise } =
      genManuallyFedIterator<ToWorker>();

    const sendJob = async ({
      job,
      workerId,
    }: {
      job: QueueJob;
      workerId: string;
    }) => {
      resolveJobPromise({ job, workerId }); // Resolve the current job promise with the job data
    };

    let updateProgress: null | ((progress: number) => Promise<void>) = null;

    (async () => {
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
          const worker = new Worker(
            `${projectId}/${specName}`,
            async (job) => {
              const jobCompleteCycle = genPromiseCycle();
              this.workerBundleById[workerId].jobCompleteCycleByJobId[job.id!] =
                jobCompleteCycle;

              sendJob({
                workerId,
                job: {
                  projectId: projectId,
                  jobId: job.id!,
                  specName,
                  jobOptionsStr: JSON.stringify(job.data.jobOptions),
                  contextId: job.data.contextId || "",
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
          await worker.waitUntilReady();

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
          this.workerBundleById[workerId].jobCompleteCycleByJobId[
            jobCompleted.jobId
          ].resolveNext(void 0);
        } else if (jobFailed) {
          if (!this.workerBundleById[workerId]) {
            throw new Error("rejectWorkerCompletePromise not initialized");
          }
          this.workerBundleById[workerId].jobCompleteCycleByJobId[
            jobFailed.jobId
          ].rejectNext(void 0);
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

const _projectServiceMap: Record<string, ProjectQueueService> = {};

export const getQueueService = () => {
  if (!_projectServiceMap["default"]) {
    _projectServiceMap["default"] = new ProjectQueueService();
  }
  return _projectServiceMap["default"];
};
