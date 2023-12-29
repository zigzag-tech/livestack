import { z } from "zod";

export const ZZJobUniqueId = z.object({
  project_id: z.string(),
  op_name: z.string(),
  job_id: z.string(),
});

export type ZZJobUniqueId = z.infer<typeof ZZJobUniqueId>;

export const ZZJobRec = <P>(jobParamsSchema: z.ZodType<P>) =>
  ZZJobUniqueId.and(
    z.object({
      init_params: jobParamsSchema.optional(),
      time_created: z.date(),
    })
  );

export type ZZJobRec<P> = z.infer<ReturnType<typeof ZZJobRec<P>>>;

export const ZZJobStatus = z.enum([
  "waiting",
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

export const ZZJobDataRec = <IO>(jobDataSchema: z.ZodType<IO>) =>
  z.object({
    job_data_id: z.string(),
    job_data: jobDataSchema,
    time_created: z.date(),
  });

// TODO: infer type from schema definition
export type ZZJobDataRec<T> = {
  job_data_id: string;
  job_data: T;
  time_created: Date;
};

export const ZZJobIOEventRec = ZZJobUniqueId.and(
  z.object({
    io_event_id: z.string(),
    job_data_id: z.string(),
    io_type: z.enum(["in", "out", "init-params"]),
    spawn_phase_id: z.string().optional().nullable(),
    time_created: z.date(),
  })
);

export type ZZJobIOEventRec = z.infer<typeof ZZJobIOEventRec>;

export const ZZJobDepRec = z.object({
  project_id: z.string(),
  parent_job_id: z.string(),
  parent_op_name: z.string(),
  child_job_id: z.string(),
  child_op_name: z.string(),
  io_event_id: z.string().optional().nullable(),
});

export type ZZJobDepRec = z.infer<typeof ZZJobDepRec>;
