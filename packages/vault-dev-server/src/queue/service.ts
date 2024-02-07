import { Observable } from "rxjs";
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

const _rawQueueBySpecName = new Map<string, Queue>();

class ProjectQueueService implements QueueServiceImplementation {
  projectId: string;
  constructor({ projectId }: { projectId: string }) {
    this.projectId = projectId;
  }

  async addJob(job: QueueJob) {
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
    projectId,
    specName,
  }: {
    projectId: string;
    specName: string;
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

  workerReportDuty(request: AsyncIterable<FromWorker>) {
    let resolveJobPromise: (value: QueueJob) => void; // Resolver function for the current job promise
    let jobPromise = new Promise<QueueJob>(
      (resolve) => (resolveJobPromise = resolve)
    ); // Initial job promise

    const sendJob = async (job: QueueJob) => {
      resolveJobPromise(job); // Resolve the current job promise with the job data
      jobPromise = new Promise((resolve) => (resolveJobPromise = resolve)); // Create a new promise for the next job
    };

    const iter: ServerStreamingMethodResult<ToWorker> = {
      async *[Symbol.asyncIterator]() {
        while (true) {
          const job = await jobPromise;
          yield { job };
        }
      },
    };

    let worker: Worker;

    let updateProgress: null | ((progress: number) => Promise<void>) = null;
    let resolveJobCompletePromise;
    let rejectJobCompletePromise;
    const jobCompletePromise = new Promise<void>((resolve, reject) => {
      resolveJobCompletePromise = resolve;
      rejectJobCompletePromise = reject;
    });

    const cleanUpAfterJob = () => {
      resolveJobCompletePromise = null;
      rejectJobCompletePromise = null;
      updateProgress = null;
    };

    (async () => {
      for await (const {
        signUp,
        progressUpdate,
        jobCompleted,
        jobFailed,
        workerStopped,
      } of request) {
        if (signUp) {
          const { projectId, specName } = signUp;
          if (projectId !== this.projectId) {
            throw new Error("Invalid projectId " + projectId);
          }
          worker = new Worker(
            `${projectId}/${specName}`,
            async (job) => {
              sendJob({
                projectId: this.projectId,
                jobId: job.id!,
                specName,
                jobOptionsStr: JSON.stringify(job.data.jobOptions),
                contextId: job.data.contextId || "",
              });

              updateProgress = job.updateProgress.bind(job);

              return await jobCompletePromise;
            },
            {
              concurrency: 1,
              connection: {
                host: process.env.REDIS_HOST || "localhost",
                port: Number(process.env.REDIS_PORT) || 6379,
              },
            }
          );
          // TODO
        } else if (progressUpdate) {
          if (!updateProgress) {
            throw new Error("updateProgress not initialized");
          } else {
            // TODO: fix typing
            (updateProgress as any)(progressUpdate.progress);
          }
        } else if (jobCompleted) {
          if (!resolveJobCompletePromise) {
            throw new Error("resolveWorkerCompletePromise not initialized");
          }
          (resolveJobCompletePromise as any)();
          cleanUpAfterJob();
        } else if (jobFailed) {
          if (!rejectJobCompletePromise) {
            throw new Error("rejectWorkerCompletePromise not initialized");
          }
          (rejectJobCompletePromise as any)(JSON.parse(jobFailed.errorStr));
          cleanUpAfterJob();
        } else if (workerStopped) {
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

export const getQueueService = ({ projectId }: { projectId: string }) => {
  if (!_projectServiceMap[projectId]) {
    _projectServiceMap[projectId] = new ProjectQueueService({ projectId });
  }
  return _projectServiceMap[projectId];
};
