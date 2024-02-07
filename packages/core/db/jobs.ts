import { Knex } from "knex";
import { JobRec, ZZJobStatusRec, ZZJobStatus } from "./rec_types";
import { v4 } from "uuid";
import {
  convertMaybePrimtiveOrArrayBack,
  handlePrimitiveOrArray,
} from "./primitives";
import { JobUniqueId } from "./db_funcs";

export async function getJobRec<T>({
  projectId,
  specName,
  jobId,
  dbConn,
}: JobUniqueId & {
  dbConn: Knex;
}) {
  const r = (await dbConn("zz_jobs")
    .select(["zz_jobs.*", "zz_job_status.status"])
    .leftJoin("zz_job_status", function () {
      this.on("zz_jobs.job_id", "=", "zz_job_status.job_id");
      this.on("zz_jobs.project_id", "=", "zz_job_status.project_id");
      this.on("zz_jobs.spec_name", "=", "zz_job_status.spec_name");
    })
    .where("zz_jobs.project_id", "=", projectId)
    .andWhere("zz_jobs.spec_name", "=", specName)
    .andWhere("zz_jobs.job_id", "=", jobId)
    .orderBy("zz_job_status.time_created", "desc")
    .first()) as
    | (JobRec<
        | T
        | {
            __primitive__: T;
          }
      > &
        Pick<ZZJobStatusRec, "status">)
    | null;

  // check if job status is what we want
  // if (jobStatus && r?.status !== jobStatus) {
  //   return null;
  // }
  if (!r) {
    return null;
  }

  return {
    ...r,
    job_params: convertMaybePrimtiveOrArrayBack(r.job_params),
  };
}

export async function updateJobStatus({
  projectId,
  specName,
  jobId,
  dbConn,
  jobStatus,
}: JobUniqueId & {
  dbConn: Knex;
  jobStatus: ZZJobStatus;
}) {
  const q = dbConn("zz_job_status").insert<ZZJobStatusRec>({
    status_id: v4(),
    project_id: projectId,
    spec_name: specName,
    job_id: jobId,
    status: jobStatus,
  });
  // console.debug(q.toString());
  await q;
}

export async function ensureJobAndInitStatusRec<T>({
  projectId,
  specName,
  jobId,
  dbConn,
  jobOptions,
}: JobUniqueId & {
  dbConn: Knex;
  jobOptions: T;
}) {
  // await dbConn.transaction(async (trx) => {
  const q = dbConn("zz_jobs")
    .insert<JobRec<T>>({
      project_id: projectId,
      spec_name: specName,
      job_id: jobId,
      job_params: handlePrimitiveOrArray(jobOptions),
    })
    .onConflict(["project_id", "spec_name", "job_id"])
    .ignore();

  // console.log(q.toString());
  await q;

  await updateJobStatus({
    projectId,
    specName,
    jobId,
    dbConn,
    jobStatus: "requested",
  });
  // });
}
