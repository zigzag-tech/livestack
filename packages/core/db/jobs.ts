import { Knex } from "knex";
import { v4 } from "uuid";
import {
  convertMaybePrimtiveOrArrayBack,
  handlePrimitiveOrArray,
} from "./primitives";
import { JobDBService, JobRec } from "@livestack/vault-interface";

export interface ZZJobUniqueId {
  project_id: string;
  spec_name: string;
  job_id: string;
}

export type ZZJobStatus =
  | "requested"
  | "running"
  | "completed"
  | "failed"
  | "waiting_children";

export interface ZZJobStatusRec extends ZZJobUniqueId {
  status: ZZJobStatus;
  time_created: Date;
}

export function wrapNullResponse<T, R>(
  f: (a: T) => Promise<{
    rec?: R;
    null_response?: {};
  }>
): (a: T) => Promise<R | null> {
  return async (a) => {
    const r = await f(a);
    if (r.null_response) {
      return null;
    }
    return r.rec as R;
  };
}

type WrapNullResponse<Fn> = Fn extends (a: infer T) => Promise<{
  rec?: infer R;
  null_response?: {};
}>
  ? (a: T) => Promise<R | null>
  : never;

type WrappedService<S> = {
  [K in keyof S]: S[K] extends (...args: infer T) => Promise<{
    rec?: infer R;
    null_response?: {};
  }>
    ? (...args: T) => Promise<R | null>
    : never;
};

export const jobDbService = (dbConn: Knex): WrappedService<JobDBService> => ({
  async GetJobRec({ projectId, specName, jobId }) {
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
      .first()) as (JobRec & Pick<ZZJobStatusRec, "status">) | null;

    // check if job status is what we want
    // if (jobStatus && r?.status !== jobStatus) {
    //   return null;
    // }
    if (!r) {
      return null;
    }

    const withJobParams = {
      ...r,
      job_params: convertMaybePrimtiveOrArrayBack(r.job_params),
    };

    const { status, ...rest } = withJobParams;
    return {
      status,
      rec: rest,
    };
  },
});

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
    .insert<JobRec>({
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
export type JobUniqueId = {
  projectId: string;
  specName: string;
  jobId: string;
};
