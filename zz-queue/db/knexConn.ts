import knex, { Knex } from "knex";
import {
  ZZJobDataRec,
  ZZJobRec,
  ZZJobStatusRec,
  ZZJobStatus,
  ZZJobIOEventRec,
} from "./rec_types";
import { v4 } from "uuid";

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

type JobUniqueId = {
  projectId: string;
  opName: string;
  jobId: string;
};

export async function getJobRec<O>({
  projectId,
  opName,
  jobId,
  dbConn,
  jobStatus,
}: JobUniqueId & {
  dbConn: Knex;
  jobStatus?: ZZJobStatus;
}) {
  const r = (await dbConn("zz_jobs")
    .select("*")
    .leftJoin("zz_job_status", function () {
      this.on("zz_jobs.job_id", "=", "zz_job_status.job_id");
      this.on("zz_jobs.project_id", "=", "zz_job_status.project_id");
      this.on("zz_jobs.op_name", "=", "zz_job_status.op_name");
    })
    .where({
      op_name: opName,
      job_id: jobId,
      project_id: projectId,
    })
    .orderBy("zz_job_status.created_at", "desc")
    .first()) as (ZZJobRec<O> & ZZJobStatusRec) | null;

  // check if job status is what we want
  if (jobStatus && r?.job_status !== jobStatus) {
    return null;
  }

  return r;
}

export async function getJobData<T>(
  jId: JobUniqueId & { dbConn: Knex; ioType: "in" | "out" }
) {
  const r = (await jId
    .dbConn("zz_job_data")
    .where({
      op_name: jId.opName,
      job_id: jId.jobId,
      project_id: jId.projectId,
      io_type: "out",
    })
    .orderBy("created_at", "desc")
    .select("*")) as ZZJobDataRec<T>[];
  return r;
}

export async function addJobDataAndIOEvent<T>({
  projectId,
  opName,
  jobId,
  dbConn,
  ioType,
  jobData,
  spawnPhaseId,
  jobDataSuffix,
}: JobUniqueId & {
  dbConn: Knex;
  ioType: "in" | "out";
  jobData: T;
  spawnPhaseId?: string;
  jobDataSuffix?: string;
}) {
  const jobDataId = `${projectId}:${opName}:${jobId}:${jobDataSuffix || v4()}`;

  await dbConn<ZZJobDataRec<T>>("zz_job_data").insert({
    job_data_id: jobDataId,
    job_data: jobData,
    time_created: new Date(),
  });

  const ioEventId = `${projectId}:${opName}:${jobId}:${jobDataSuffix || v4()}`;
  // insert input event rec
  await dbConn<ZZJobIOEventRec>("zz_job_io_events").insert(
    {
      project_id: projectId,
      op_name: opName,
      job_id: jobId,
      io_type: ioType,
      io_event_id: ioEventId,
      job_data_id: jobDataId,
      spawn_phase_id: spawnPhaseId || null,
    },
    ["io_event_id"]
  );

  return { ioEventId, jobDataId };
}

export async function updateJobStatus({
  projectId,
  opName,
  jobId,
  dbConn,
  jobStatus,
}: JobUniqueId & {
  dbConn: Knex;
  jobStatus: ZZJobStatus;
}) {
  await dbConn("zz_job_status").insert({
    project_id: projectId,
    op_name: opName,
    job_id: jobId,
    job_status: jobStatus,
  });
}

export async function addJobRec<T>({
  projectId,
  opName,
  jobId,
  dbConn,
  jobStatus = "waiting",
  initParams,
}: JobUniqueId & {
  dbConn: Knex;
  jobStatus?: ZZJobStatus;
  initParams: T;
}) {
  await dbConn("zz_jobs").insert({
    project_id: projectId,
    op_name: opName,
    job_id: jobId,
    job_status: jobStatus,
  });

  const jobDataSuffix = "init_input";

  const { jobDataId, ioEventId } = await addJobDataAndIOEvent({
    projectId,
    opName,
    jobId,
    dbConn,
    ioType: "in",
    jobData: initParams,
    jobDataSuffix,
  });

  return {
    jobDataId,
    ioEventId,
  };
}

export async function ensureJobDependencies({
  projectId,
  parentJobId,
  parentOpName,
  childJobId,
  childOpName,
  dbConn,
}: {
  projectId: string;
  parentJobId: string;
  parentOpName: string;
  childJobId: string;
  childOpName: string;
  io_event_id: string;
  dbConn: Knex;
}) {
  await dbConn.raw(
    `
    INSERT INTO "zz_job_deps" ("project_id", "parent_op_name", "parent_job_id", "child_op_name", "child_job_id")
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT ("project_id", "parent_op_name", "parent_job_id", "child_op_name", "child_job_id") DO NOTHING
    `,
    [projectId, parentJobId, childJobId]
  );
}

// export async function ensureJobDependencies({
//   parentJobId,
//   childJobId,
//   dbConn,
//   projectId,
// }: {
//   parentJobId: string;
//   childJobId: string;
//   dbConn: Knex;
//   projectId: string;
// }) {
//   await dbConn.raw(
//     `
//     INSERT INTO "job_deps" ("project_id", "parent_job_id", "child_job_id")
//     VALUES (?, ?, ?)
//     ON CONFLICT ("project_id", "parent_job_id", "child_job_id") DO NOTHING
//     `,
//     [projectId, parentJobId, childJobId]
//   );
// }
