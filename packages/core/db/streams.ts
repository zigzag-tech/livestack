import { ZZJobStreamConnectorRec, DataStreamRec } from "./streams";
import { Knex } from "knex";

export async function ensureStreamRec({
  projectId,
  streamId,
  dbConn,
}: {
  projectId: string;
  dbConn: Knex;
  streamId: string;
}) {
  await dbConn<DataStreamRec>("zz_streams")
    .insert({
      project_id: projectId,
      stream_id: streamId,
      time_created: new Date(),
    })
    .onConflict(["project_id", "stream_id"])
    .ignore();
}

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

export async function getJobStreamConnectorRecs({
  projectId,
  dbConn,
  jobId,
  key,
  connectorType,
}: {
  projectId: string;
  dbConn: Knex;
  jobId: string;
  connectorType?: "in" | "out";
  key?: string;
}) {
  if (key && !connectorType) {
    throw new Error("connectorType must be provided if key is provided");
  }

  const q = dbConn<ZZJobStreamConnectorRec>("zz_job_stream_connectors")
    .where("project_id", "=", projectId)
    .andWhere("job_id", "=", jobId);
  if (key) {
    q.andWhere("key", "=", key);
  }
  if (connectorType) {
    q.andWhere("connector_type", "=", connectorType);
  }
  const r = await q.select("*");
  return r;
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
