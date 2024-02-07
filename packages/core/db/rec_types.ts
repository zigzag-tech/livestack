export interface ZZJobUniqueId {
  project_id: string;
  spec_name: string;
  job_id: string;
}

export interface JobRec<P> extends ZZJobUniqueId {
  job_params?: P;
  time_created: Date;
}

export type ZZJobStatus = "requested" | "running" | "completed" | "failed" | "waiting_children";

export interface ZZJobStatusRec extends ZZJobUniqueId {
  status: ZZJobStatus;
  time_created: Date;
}

export interface ZZDatapointRec<T> {
  project_id: string;
  stream_id: string;
  datapoint_id: string;
  data: T;
  job_id: string | null;
  job_output_key: string | null;
  connector_type: string;
  time_created: Date;
}

  connector_type: string | null;
  time_created: Date;
};

export interface DataStreamRec {
  project_id: string;
  stream_id: string;
  time_created: Date;
}

export interface ZZJobStreamConnectorRec {
  project_id: string;
  job_id: string;
  stream_id: string;
  key: string;
  connector_type: "in" | "out";
  time_created: Date;
}

export interface ZZJobRelationRec {
  project_id: string;
  parent_job_id: string;
  child_job_id: string;
  time_created: Date;
}
