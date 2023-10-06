import {
  JobsOptions,
  QueueEvents,
  QueueEventsOptions,
  WorkerOptions,
} from "bullmq";
import { GenericRecordType, QueueName } from "./workerCommon";
import { Queue, Job } from "bullmq";
import { getLogger } from "../utils/createWorkerLogger";
import {
  _upsertAndMergeJobLogByIdAndType,
  getJobLogByIdAndType,
} from "../db/knexConn";
import { Knex } from "knex";
import { v4 } from "uuid";
import Redis from "ioredis";
import { first } from "lodash";

const queueMap = new Map<
  QueueName<GenericRecordType>,
  ReturnType<typeof createAndReturnQueue>
>();

export const getMicroworkerQueueByName = <
  JobDataType,
  JobReturnType,
  T extends GenericRecordType
>(
  p: Parameters<typeof createAndReturnQueue<JobDataType, JobReturnType, T>>[0]
  // & {
  //   queueNamesDef: T;
  // }
): ReturnType<typeof createAndReturnQueue<JobDataType, JobReturnType, T>> => {
  const {
    // queueNamesDef,
    queueName,
  } = p;
  // if (!Object.values(queueNamesDef).includes(queueName)) {
  //   throw new Error(`Can not handle queueName ${queueName}!`);
  // }
  const existing = queueMap.get(queueName) as ReturnType<
    typeof createAndReturnQueue<JobDataType, JobReturnType, T>
  >;
  if (existing) {
    return existing;
  } else {
    return createAndReturnQueue<JobDataType, JobReturnType, T>(p);
  }
};

function createAndReturnQueue<
  JobDataType,
  JobReturnType,
  T extends GenericRecordType = GenericRecordType
>({
  projectId,
  queueName,
  workerOptions,
  db,
}: {
  projectId: string;
  queueName: QueueName<T>;
  workerOptions?: WorkerOptions;
  db: Knex;
}) {
  const queue = new Queue<{ firstInput: JobDataType }, JobReturnType>(
    queueName,
    workerOptions
  );
  const logger = getLogger(`wkr:${queueName}`);

  // return queue as Queue<JobDataType, JobReturnType>;
  const addJob = async ({
    jobId,
    firstInput,
    bullMQJobsOpts,
  }: {
    jobId: string;
    firstInput: JobDataType;
    bullMQJobsOpts?: JobsOptions;
  }) => {
    // force job id to be the same as name
    const j = await queue.add(
      jobId,
      { firstInput },
      { ...bullMQJobsOpts, jobId: jobId }
    );
    logger.info(
      `Added job with ID: ${j.id}, ${j.queueName} ` +
        `${JSON.stringify(j.data, longStringTruncator)}`
    );

    await _upsertAndMergeJobLogByIdAndType({
      projectId,
      jobType: queueName,
      jobId: j.id!,
      jobData: { firstInput },
      dbConn: db,
    });

    return j;
  };

  const getJob = async (jobId: string) => {
    const j = await queue.getJob(jobId);
    if (!j) {
      const dbJ = await getJobLogByIdAndType({
        jobType: queueName,
        jobId,
        projectId,
        dbConn: db,
      });
      if (dbJ) {
        return {
          id: dbJ.job_id,
          firstInput: dbJ.job_data as { firstInput: JobReturnType },
        };
      }
    }
    return j || null;
  };

  const enqueueJobAndGetResult = async ({
    jobName: jobId,
    initJobData,
    queueEventsOptions,
  }: {
    jobName?: string;
    initJobData: JobDataType;
    queueEventsOptions?: QueueEventsOptions;
  }): Promise<JobReturnType> => {
    if (!jobId) {
      jobId = `${queueName}-${v4()}`;
    }

    console.info(`Enqueueing job ${jobId} with data:`, initJobData);
    const queueEvents = new QueueEvents(queueName, {
      ...queueEventsOptions,
    });

    const job = await addJob({
      jobId,
      firstInput: initJobData,
    });

    try {
      await job.waitUntilFinished(queueEvents);
      const result = await Job.fromId(queue, jobId);
      return result!.returnvalue as JobReturnType;
    } finally {
      await queueEvents.close();
    }
  };

  const cancelJob = async (jobId: string) => {
    const job = await queue.getJob(jobId);
    if (!job) {
      throw new Error(`Job ${jobId} not found!`);
    }

    // signal to worker to cancel
    const redis = new Redis();
    redis.del(`last-time-job-alive-${jobId}`);
  };

  const pingAlive = async (jobId: string) => {
    const redis = new Redis();
    await redis.set(`last-time-job-alive-${jobId}`, Date.now());
  };

  const funcs = {
    addJob,
    enqueueJobAndGetResult,
    getJob,
    cancelJob,
    pingAlive,
    _rawQueue: queue,
  };

  // todo: fix typing
  queueMap.set(queueName, funcs as any);

  return funcs;
}

export const longStringTruncator = (k: string, v: unknown) => {
  // truncate long strings
  if (typeof v === "string" && v.length > 100) {
    return `${v.slice(0, 100)}...`;
  }
  return v;
};
