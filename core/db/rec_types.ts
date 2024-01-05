import { z } from "zod";

// Removed ZZJobUniqueId as it's no longer needed.

// Removed ZZJobUniqueId type as it's no longer needed.

export const ZZJobRec = z.object({
  project_id: z.string(),
  pipe_name: z.string(),
  job_id: z.string(),
  job_params: z.any().nullable(),
  time_created: z.date(),
});

export type ZZJobRec = z.infer<typeof ZZJobRec>;

// Removed ZZJobStatus enum as it's no longer needed.

export const ZZJobStatusRec = z.object({
  status_id: z.string(),
  project_id: z.string(),
  job_id: z.string(),
  status: z.string(),
  time_created: z.date(),
});

export type ZZJobStatusRec = z.infer<typeof ZZJobStatusRec>;

// Removed ZZJobDataRec as it's no longer needed.

// TODO: infer type from schema definition
// Removed ZZJobDataRec type as it's no longer needed.

// Removed ZZJobIOEventRec as it's no longer needed.

// Removed ZZJobIOEventRec type as it's no longer needed.

// Added new types to reflect the current schema.
export const ZZDatapointRec = z.object({
  project_id: z.string(),
  stream_id: z.string(),
  datapoint_id: z.string(),
  data: z.any(),
  job_id: z.string().nullable(),
  job_output_key: z.string().nullable(),
  connector_type: z.string(),
  time_created: z.date(),
});

export type ZZDatapointRec = z.infer<typeof ZZDatapointRec>;

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
  connector_type: z.string(),
  time_created: z.date(),
});

export type ZZJobStreamConnectorRec = z.infer<typeof ZZJobStreamConnectorRec>;
