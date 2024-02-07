import { z } from "zod";

export const ZZJobUniqueId = z.object({
  project_id: z.string(),
  spec_name: z.string(),
  job_id: z.string(),
});

export type ZZJobUniqueId = z.infer<typeof ZZJobUniqueId>;

export const JobRec = <P>(jobOptionsSchema: z.ZodType<P>) =>
  ZZJobUniqueId.merge(
    z.object({
      job_params: jobOptionsSchema.optional(),
      time_created: z.date(),
    })
  );

export type JobRec<P> = z.infer<ReturnType<typeof JobRec<P>>>;

export const ZZJobStatus = z.enum([
  "requested",
  "running",
  "completed",
  "failed",
  "waiting_children",
]);
export type ZZJobStatus = z.infer<typeof ZZJobStatus>;

export const ZZJobStatusRec = ZZJobUniqueId.merge(
  z.object({
    status: ZZJobStatus,
    time_created: z.date(),
  })
);

export type ZZJobStatusRec = z.infer<typeof ZZJobStatusRec>;

export const ZZDatapointRec = <IO>(jobDataSchema: z.ZodType<IO>) =>
  z.object({
    project_id: z.string(),
    stream_id: z.string(),
    datapoint_id: z.string(),
    data: jobDataSchema,
    job_id: z.string().nullable(),
    job_output_key: z.string().nullable(),
    connector_type: z.string(),
    time_created: z.date(),
  });

// TODO: infer type from schema definition
export type ZZDatapointRec<T> = {
  project_id: string;
  stream_id: string;
  datapoint_id: string;
  data: T;
  job_id: string | null;
  job_output_key: string | null;
  connector_type: string | null;
  time_created: Date;
};

export const DataStreamRec = z.object({
  project_id: z.string(),
  stream_id: z.string(),
  time_created: z.date(),
});

export type DataStreamRec = z.infer<typeof DataStreamRec>;

export const ZZJobStreamConnectorRec = z.object({
  project_id: z.string(),
  job_id: z.string(),
  stream_id: z.string(),
  key: z.string(),
  connector_type: z.enum(["in", "out"]),
  time_created: z.date(),
});

export type ZZJobStreamConnectorRec = z.infer<typeof ZZJobStreamConnectorRec>;

export const ZZJobRelationRec = z.object({
  project_id: z.string(),
  parent_job_id: z.string(),
  child_job_id: z.string(),
  time_created: z.date(),
});

export type ZZJobRelationRec = z.infer<typeof ZZJobRelationRec>;
