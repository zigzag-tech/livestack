export interface ZZJob<P> {
  project_id: string;
  op_name: string;
  job_id: string;
  init_params: P | null; // JSONB column, replace 'any' with a more specific type if known
  time_created: Date;
}

export interface ZZJobData<IO> {
  job_data_id: string;
  job_data: IO; // JSONB column, replace 'any' with a more specific type if known
  time_created: Date;
}

// multi-phase spawn job

export type ZZJobIOEvent = {
  io_event_id: string;
  project_id: string;
  op_name: string;
  job_id: string;
  job_data_id: string;
  time_created: Date;
} & (
  | {
      io_type: "spawn_output";
      spawn_phase_id: string; // Nullable column
    }
  | {
      io_type: "input" | "output";
      spawn_phase_id: null;
    }
);

export interface ZZJobDep {
  project_id: string;
  parent_job_id: string;
  parent_op_name: string;
  child_job_id: string;
  child_op_name: string;
  io_event_id: string | null; // Nullable column
}
