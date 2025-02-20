import {
  QueueServiceImplementation,
  QueueJob,
  FromWorker,
  ToWorker,
} from "@livestack/vault-interface";
import { InitInstanceParams } from "@livestack/vault-interface";
import { Queue, Worker } from "bullmq";
import { genPromiseCycle, genManuallyFedIterator } from "@livestack/shared";
import { CallContext } from "nice-grpc";
import { v4 } from "uuid";
import { createClient } from "redis";
import { escapeColon, getCapacityManager } from "../capacity/manager";
import { promises as fs } from "fs";
import { REDIS_MEMORY_SERVER_P } from "../ensureRedisMemoryServer";
const _rawQueueBySpecName = new Map<string, Queue>();

class QueueServiceByProject implements QueueServiceImplementation {
  //   projectUuid: string;
  private logEntriesExist: boolean;
  authMiddleware?: (context: CallContext) => Promise<void>;
  onJobAssignedToWorker?: (job: QueueJob, ctx: CallContext) => Promise<void>;
  constructor(p?: {
    authMiddleware?: (context: CallContext) => Promise<void>;
    onJobAssignedToWorker?: (job: QueueJob, ctx: CallContext) => Promise<void>;
  }) {
    const { authMiddleware } = p || {};
    // this.projectUuid = projectUuid;
    this.authMiddleware = authMiddleware;
    this.onJobAssignedToWorker = p?.onJobAssignedToWorker;
    this.logEntriesExist = false;
  }
  private redisClientP = ((async ()=>{
    const {host, port} = await REDIS_MEMORY_SERVER_P;
    const redisClient = createClient({
      socket: {
        host,
        port,
      },
    });
    await redisClient.connect();
    return redisClient;
  }))();

  async initInstance(
    request: InitInstanceParams,
    context: CallContext
  ): Promise<{ instanceId?: string | undefined }> {
    const instanceId = v4();
    // console.log(`Entering initInstance`)
    return { instanceId };
  }

  // private logFilePath = path.join(__dirname, 'workerLogs.json');
  private logFilePath = 'workerLogs.json';

  async appendLog(entries: any){
    const data = JSON.stringify(entries, null, 2);
    const delimiter = this.logEntriesExist ? ',\n' : '';
    this.logEntriesExist = true;
    fs.appendFile(this.logFilePath, data);
  }

  async logWorkers(queue: Queue, projectUuid: string){
    const workers = await queue.getWorkers();
    const capacity = workers.length;
    if(capacity > 0){
      const logEntries = [];
      for (const worker of workers) {
        logEntries.push({
          projectUuid: projectUuid,
          queueName: queue.name,
          workerId: worker.id
        });
      }
      this.appendLog(logEntries);
    }
  }

  async logAllWorkers(projectUuid: string){

    // console.log(`Logging workers for project: ${projectUuid}`);
    for(const[key, queue] of _rawQueueBySpecName.entries()){
      if(key.startsWith(`${projectUuid}/`)){
        await this.logWorkers(queue, projectUuid);
      }
    }

  }

  async addJob(job: QueueJob, context: CallContext) {
    // if (job.projectUuid !== this.projectUuid) {
    //   throw new Error("Invalid projectUuid " + job.projectUuid);
    // }

    if (this.authMiddleware) {
      await this.authMiddleware(context);
    }
    const queue = this.getQueue(job);
    // console.debug("addjob", queue.name, job);

    // get current capacity
    // const client = await this.redisClientP;
    // const projectUuidN = escapeColon(job.projectUuid);
    // const specNameN = escapeColon(job.specName);

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
    const countOfTotalJobsRequiringCapacity =
      (await queue.getWaitingCount()) + (await queue.getActiveCount());

    const workers = await queue.getWorkers();
    const capacity = workers.length;

    if (
      countOfTotalJobsRequiringCapacity > 0 &&
      countOfTotalJobsRequiringCapacity > capacity
    ) {
      const diff = countOfTotalJobsRequiringCapacity - capacity;
      console.debug(
        // `Queue ${job.specName} for project ${job.projectUuid} has ${countOfTotalJobsRequiringCapacity} waiting+active jobs, while capacity is at ${capacity}. \nAttempting to increase capacity by ${diff}.`
      );
      for (let i = 0; i < diff; i++) {
        await getCapacityManager().increaseCapacity({
          projectUuid: job.projectUuid,
          specName: job.specName,
          by: 1,
        });
      }
    }

    // await this.logAllWorkers(job.projectUuid);
    return {};
  }

  private getQueue({
    specName,
    projectUuid,
  }: {
    specName: string;
    projectUuid: string;
  }) {
    const queueId = `${projectUuid}/${specName}`;
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
    // console.log(`Entering reportAsWorker`)
    const { iterator: iterServerMsg, resolveNext: resolveJobPromise } =
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
          const { projectUuid, specName } = signUp;
          //   if (projectUuid !== this.projectUuid) {
          //     throw new Error("Invalid projectUuid " + projectUuid);
          //   }
          // console.debug("Adding worker", `${projectUuid}/${specName}`);
          const worker = new Worker(
            `${projectUuid}/${specName}`,
            async (job) => {
              const jobCompleteCycle = genPromiseCycle();
              this.workerBundleById[workerId].jobCompleteCycleByJobId[job.id!] =
                jobCompleteCycle;
              await sendJob({
                workerId,
                job: {
                  projectUuid: projectUuid,
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
            `livestack/${(await getCapacityManager()).sessionId}/zz_capacity`,
            `${projectUuid}:${specName}`,
            "1",
          ]);

          await worker.waitUntilReady();
          const abortListener = async () => {
            // decrement the capacity for this worker by 1
            await client.sendCommand([
              "HINCRBY",
              `livestack/${await getCapacityManager()}/zz_capacity`,
              `${projectUuid}:${specName}`,
              "-1",
            ]);

            console.debug(
              "Worker stopped for",
              specName,
              ", id:",
              workerId,
              "."
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

    return iterServerMsg;
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
