export type GenericRecordType = { [x: string]: string };
export type QueueName<T extends GenericRecordType> = T[keyof T];
export type RawQueueJobData<JobOptions> = {
  jobOptions: JobOptions;
  contextId: string | null;
};
