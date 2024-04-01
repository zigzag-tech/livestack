import { Knex } from "knex";

export async function ensureJobStreamConnectorRec({
  projectId,
  streamId,
  dbConn,
  jobId,
  key,
  connectorType,
}: {
  projectId: string;
  dbConn: Knex;
  streamId: string;
  jobId: string;
  key: string;
  connectorType: "in" | "out";
}) {
  await dbConn<ZZJobStreamConnectorRec>("zz_job_stream_connectors")
    .insert({
      project_id: projectId,
      job_id: jobId,
      stream_id: streamId,
      key,
      connector_type: connectorType,
      time_created: new Date(),
    })
    .onConflict<keyof ZZJobStreamConnectorRec>([
      "project_id",
      "job_id",
      "key",
      "connector_type",
    ])
    .ignore();
}

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
