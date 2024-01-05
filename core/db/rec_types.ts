import { z } from "zod";

export const ZZJobUniqueId = z.object({
  project_id: z.string(),
  pipe_name: z.string(),
  job_id: z.string(),
});

export type ZZJobUniqueId = z.infer<typeof ZZJobUniqueId>;

export const ZZJobRec = <P>(jobParamsSchema: z.ZodType<P>) =>
  ZZJobUniqueId.and(
    z.object({
      job_params: jobParamsSchema.optional(),
      time_created: z.date(),
    })
  );

export type ZZJobRec<P> = z.infer<ReturnType<typeof ZZJobRec<P>>>;

export const ZZJobStatus = z.enum([
  "requested",
  "running",
  "completed",
  "failed",
  "waiting_children",
]);
export type ZZJobStatus = z.infer<typeof ZZJobStatus>;

export const ZZJobStatusRec = ZZJobUniqueId.and(
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
  connector_type: string;
  time_created: Date;
};

export const ZZStreamRec = z.object({
  project_id: z.string(),
  stream_id: z.string(),
  time_created: z.date(),
});

export type ZZStreamRec = z.infer<typeof ZZStreamRec>;

export const ZZJobStreamConnectorRec = z.object({
  project_id: z.string(),
  job_id: z.string(),
  stream_id: z.string(),
  key: z.string(),
  connector_type: z.enum(["input", "output"]),
  time_created: z.date(),
});

export type ZZJobStreamConnectorRec = z.infer<typeof ZZJobStreamConnectorRec>;
