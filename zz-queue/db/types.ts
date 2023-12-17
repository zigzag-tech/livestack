import { z } from "zod";

export const ZZJob = <P extends z.ZodTypeAny>(initParamsSchema: P) =>
  z.object({
    project_id: z.string(),
    op_name: z.string(),
    job_id: z.string(),
    init_params: initParamsSchema.optional(),
    time_created: z.date(),
  });

export type ZZJob<P extends z.ZodTypeAny> = z.infer<
  ReturnType<typeof ZZJob<P>>
>;

export const ZZJobData = <IO>(jobDataSchema: z.ZodType<IO, any, any>) =>
  z.object({
    job_data_id: z.string(),
    job_data: jobDataSchema,
    time_created: z.date(),
  });

export type ZZJobData<IO> = z.infer<ReturnType<typeof ZZJobData<IO>>>;

export const ZZJobIOEvent = z.object({
  io_event_id: z.string(),
  project_id: z.string(),
  op_name: z.string(),
  job_id: z.string(),
  job_data_id: z.string(),
  io_type: z.enum(["in", "out"]),
  spawn_phase_id: z.string().optional().nullable(),
  time_created: z.date(),
});

export type ZZJobIOEvent = z.infer<typeof ZZJobIOEvent>;

export const ZZJobDep = z.object({
  project_id: z.string(),
  parent_job_id: z.string(),
  parent_op_name: z.string(),
  child_job_id: z.string(),
  child_op_name: z.string(),
  io_event_id: z.string().optional().nullable(),
});

export type ZZJobDep = z.infer<typeof ZZJobDep>;
