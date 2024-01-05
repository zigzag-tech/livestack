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
  pipeName: string;
  jobId: string;
};

export async function getJobRec<T>({
  projectId,
  pipeName,
  jobId,
  dbConn,
}: // jobStatus,
JobUniqueId & {
  dbConn: Knex;
  // jobStatus?: ZZJobStatus;
}) {
  const r = (await dbConn("zz_jobs")
    .select(["zz_jobs.*", "zz_job_status.status"])
    .leftJoin("zz_job_status", function () {
      this.on("zz_jobs.job_id", "=", "zz_job_status.job_id");
      this.on("zz_jobs.project_id", "=", "zz_job_status.project_id");
      this.on("zz_jobs.pipe_name", "=", "zz_job_status.pipe_name");
    })
    .where("zz_jobs.project_id", "=", projectId)
    .andWhere("zz_jobs.pipe_name", "=", pipeName)
    .andWhere("zz_jobs.job_id", "=", jobId)
    .orderBy("zz_job_status.time_created", "desc")
    .first()) as
    | (ZZJobRec<
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
    job_params: convertMaybePrimtiveBack(r.job_params),
  };
}

export async function getJobDataAndIoEvents<T>({
  limit = 100,
  ioType,
  order = "desc",
  dbConn,
  ...jId
}: JobUniqueId & {
  dbConn: Knex;
  ioType: "in" | "out" | "init-params";
  order?: "asc" | "desc";
  limit?: number;
}) {
  const r = await dbConn<ZZJobIOEventRec>("zz_job_io_events")
    .join<
      ZZJobDataRec<
        | T
        | {
            __primitive__: T;
          }
      >
    >("zz_job_data", function () {
      this.on("zz_job_data.job_data_id", "=", "zz_job_io_events.job_data_id");
    })
    .where("zz_job_io_events.project_id", "=", jId.projectId)
    .andWhere("zz_job_io_events.pipe_name", "=", jId.pipeName)
    .andWhere("zz_job_io_events.job_id", "=", jId.jobId)
    .andWhere("zz_job_io_events.io_type", "=", ioType)
    .orderBy("zz_job_io_events.time_created", order)
    .limit(limit)
    .select("*");
  if (r.length === 0) {
    console.error("Job data not found", jId, ioType);
    throw new Error(`Job data for ${jId.jobId} not found!`);
  }
  return r.map((rec) => ({
    ioEventId: rec.io_event_id,
    data: convertMaybePrimtiveBack(rec.job_data),
  }));
}

export async function addJobDataAndIOEvent<T>({
  projectId,
  pipeName,
  jobId,
  dbConn,
  ioType,
  jobData,
  spawnPhaseId,
  jobDataSuffix,
}: JobUniqueId & {
  dbConn: Knex;
  ioType: "in" | "out" | "init-params";
  jobData: T;
  spawnPhaseId?: string;
  jobDataSuffix?: string;
}) {
  const jobDataId = `${projectId}:${pipeName}:${jobId}:${
    jobDataSuffix || v4()
  }`;

  await dbConn<
    ZZJobDataRec<
      | T
      | {
          __primitive__: T;
        }
    >
  >("zz_job_data")
    .insert({
      job_data_id: jobDataId,
      job_data: handlePrimitive(jobData),
      time_created: new Date(),
    })
    .onConflict(["job_data_id"])
    .merge();

  const ioEventId = `${projectId}:${pipeName}:${jobId}:${
    jobDataSuffix || v4()
  }`;
  // insert input event rec
  await dbConn<ZZJobIOEventRec>("zz_job_io_events")
    .insert(
      {
        project_id: projectId,
        pipe_name: pipeName,
        job_id: jobId,
        io_type: ioType,
        io_event_id: ioEventId,
        job_data_id: jobDataId,
        spawn_phase_id: spawnPhaseId || null,
      },
      ["io_event_id"]
    )
    .onConflict(["io_event_id"])
    .merge();

  return { ioEventId, jobDataId };
}

function handlePrimitive<T>(d: T) {
  // stringify if jobData is primitive
  let jobDataT: T | { __primitive__: T };
  if (typeof d !== "object" || d === null) {
    jobDataT = {
      __primitive__: d,
    };
  } else {
    jobDataT = d;
  }
  return jobDataT;
}

function convertMaybePrimtiveBack<T>(
  p:
    | T
    | {
        __primitive__: T;
      }
): T {
  if (typeof p === "object" && p !== null && "__primitive__" in p) {
    return p.__primitive__;
  } else {
    return p;
  }
}

export async function updateJobStatus({
  projectId,
  pipeName,
  jobId,
  dbConn,
  jobStatus,
}: JobUniqueId & {
  dbConn: Knex;
  jobStatus: ZZJobStatus;
}) {
  await dbConn("zz_job_status").insert<ZZJobStatusRec>({
    status_id: v4(),
    project_id: projectId,
    pipe_name: pipeName,
    job_id: jobId,
    status: jobStatus,
  });
}

export async function ensureJobAndInitStatusRec<T>({
  projectId,
  pipeName,
  jobId,
  dbConn,
  jobParams,
}: JobUniqueId & {
  dbConn: Knex;
  jobParams: T;
}) {
  await dbConn("zz_jobs")
    .insert<ZZJobRec<T>>({
      project_id: projectId,
      pipe_name: pipeName,
      job_id: jobId,
      job_params: handlePrimitive(jobParams),
    })
    .onConflict(["project_id", "pipe_name", "job_id"])
    .ignore();

  await updateJobStatus({
    projectId,
    pipeName,
    jobId,
    dbConn,
    jobStatus: "waiting",
  });

  const jobDataSuffix = "init_input";

  const { jobDataId, ioEventId } = await addJobDataAndIOEvent({
    projectId,
    pipeName,
    jobId,
    dbConn,
    ioType: "init-params",
    jobData: jobParams,
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
  parentPipeName,
  childJobId,
  childPipeName,
  dbConn,
}: {
  projectId: string;
  parentJobId: string;
  parentPipeName: string;
  childJobId: string;
  childPipeName: string;
  io_event_id: string;
  dbConn: Knex;
}) {
  await dbConn.raw(
    `
    INSERT INTO "zz_job_deps" ("project_id", "parent_pipe_name", "parent_job_id", "child_pipe_name", "child_job_id")
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT ("project_id", "parent_pipe_name", "parent_job_id", "child_pipe_name", "child_job_id") DO NOTHING
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
