export interface ZZJobData {
  job_data_id: string;
  job_data: any; // JSONB column, replace 'any' with a more specific type if known
  time_created: Date;
}

export interface ZZJob {
  project_id: string;
  op_name: string;
  job_id: string;
  init_params?: any; // JSONB column, replace 'any' with a more specific type if known
  time_created: Date;
}

export interface ZZJobIOEvent {
  io_event_id: string;
  project_id: string;
  op_name: string;
  job_id: string;
  io_type: string;
  job_data_id: string;
}

export interface ZZJobDep {
  project_id: string;
  parent_job_id: string;
  parent_op_name: string;
  child_job_id: string;
  child_op_name: string;
  io_event_id?: string; // Nullable column
}
