import {
  QueueServiceImplementation,
  QueueJob,
} from "@livestack/vault-interface";
import { Queue } from "bullmq";
const _rawQueueBySpecName = new Map<string, Queue>();
const getQueue = ({
  projectId,
  specName,
}: {
  projectId: string;
  specName: string;
}) => {
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
};
export const queueService: QueueServiceImplementation = {
  addJob: async (job: QueueJob) => {
    const queue = getQueue(job);
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
  },
};
