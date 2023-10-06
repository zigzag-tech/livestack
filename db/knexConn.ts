import knex, { Knex } from "knex";
import { GenericRecordType, QueueName } from "../microworkers/workerCommon";
import { WorkerListener } from "bullmq";

export const getDatabaseInstance = ({
  host,
  port,
  user,
  password,
  database,
}: {
  host: string;
  port: string;
  user: string;
  password: string;
  database: string;
}) =>
  knex({
    client: "postgresql",
    connection: {
      host,
      port: parseInt(port),
      user,
      password,
      database,
    },
    useNullAsDefault: true,
  });

export type JobLog<T extends GenericRecordType> = {
  project_id: string;
  job_type: QueueName<T>;
  job_id: string;
  job_data: any; // JSONB type
  job_status: keyof WorkerListener | "waiting_children";
  time_created: Date;
  time_updated: Date;
};

export async function getJobLogByIdAndType<T extends GenericRecordType>({
  projectId,
  jobType,
  jobId,
  dbConn,
  jobStatus,
}: {
  projectId: string;
  jobType: QueueName<T>;
  jobId: string;
  dbConn: Knex;
  jobStatus?: keyof WorkerListener | "waiting_children";
}) {
  const r = (await dbConn("jobs_log")
    .select("*")
    .where({ job_type: jobType, job_id: jobId, project_id: projectId })
    .first()) as JobLog<T> | null;
  if (jobStatus && r?.job_status === jobStatus) {
    return r;
  } else {
    return null;
  }
}

export async function _upsertAndMergeJobLogByIdAndType<
  T extends GenericRecordType
>({
  projectId,
  jobType,
  jobId,
  jobData: newJobData,
  dbConn,
  jobStatus,
}: {
  projectId: string;
  jobType: QueueName<T>;
  jobId: string;
  jobData?: any;
  dbConn: Knex;
  jobStatus?: keyof WorkerListener | "waiting_children";
}) {
  const upsertR = await dbConn.transaction(async (trx) => {
    const updateR = await trx.raw(
      `
  INSERT INTO "jobs_log" ("job_type", "job_id", "project_id", "job_data", "job_status")
  VALUES (?, ?, ?, ?::jsonb, ?)
  ON CONFLICT ("job_type", "job_id", "project_id") DO UPDATE
  SET "job_data" = "jobs_log"."job_data" || EXCLUDED."job_data",
      "job_status" = COALESCE(EXCLUDED."job_status", "jobs_log"."job_status")
`,
      [
        jobType,
        jobId,
        projectId,
        JSON.stringify({ ...newJobData }),
        jobStatus || "active",
      ]
    );
  });
  // return upsertR;
}
