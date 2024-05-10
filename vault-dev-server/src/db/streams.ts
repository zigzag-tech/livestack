import { Knex } from "knex";

export async function ensureJobStreamConnectorRec({
  projectUuid,
  streamId,
  dbConn,
  jobId,
  key,
  connectorType,
}: {
  projectUuid: string;
  dbConn: Knex;
  streamId: string;
  jobId: string;
  key: string;
  connectorType: "in" | "out";
}) {
  await dbConn<LiveJobStreamConnectorRec>("zz_job_stream_connectors")
    .insert({
      project_uuid: projectUuid,
      job_id: jobId,
      stream_id: streamId,
      key,
      connector_type: connectorType,
      time_created: new Date(),
    })
    .onConflict<keyof LiveJobStreamConnectorRec>([
      "project_uuid",
      "job_id",
      "key",
      "connector_type",
    ])
    .ignore();
}

export interface DataStreamRec {
  project_uuid: string;
  stream_id: string;
  time_created: Date;
}

export interface LiveJobStreamConnectorRec {
  project_uuid: string;
  job_id: string;
  stream_id: string;
  key: string;
  connector_type: "in" | "out";
  time_created: Date;
}
