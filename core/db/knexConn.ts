import { ZZDatapointRec } from "./rec_types";
import knex, { Knex } from "knex";
import { ZZJobRec, ZZJobStatusRec, ZZJobStatus } from "./rec_types";
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

export async function getJobDatapoints<T>({
  ioType,
  order = "desc",
  dbConn,
  key,
  limit = 100,
  ...jId
}: { dbConn: Knex } & JobUniqueId & {
    ioType: "in" | "out";
    order?: "asc" | "desc";
    limit?: number;
    key: string;
  }) {
  const r = await dbConn<
    ZZDatapointRec<
      | T
      | {
          __primitive__: T;
        }
    >
  >("zz_datapoints")
    // .join<ZZJobRec>("zz_streams", function () {
    //   this.on("zz_datapoints.project_id", "=", "zz_streams.project_id");
    // })
    // .where("zz_job_io_events.project_id", "=", jId.projectId)
    // .andWhere("zz_job_io_events.pipe_name", "=", jId.pipeName)
    // .andWhere("zz_job_io_events.job_id", "=", jId.jobId)
    // .andWhere("zz_job_io_events.io_type", "=", ioType)
    .orderBy("zz_datapoints.time_created", order)
    .limit(limit)
    .select("*");
  // if (r.length === 0) {
  //   console.error("Job datapoints not found", jId, ioType);
  //   throw new Error(`Job datapoint for ${jId.jobId} not found!`);
  // }
  return r.map((rec) => ({
    datapointId: rec.datapoint_id,
    data: convertMaybePrimtiveBack(rec.data),
  }));
}

export async function addDatapoint<T>({
  projectId,
  dbConn,
  datapoint,
}: {
  dbConn: Knex;
  projectId: string;
  datapoint: {
    data: T;
    job_id: string | null;
    job_output_key: string | null;
    connector_type: "in" | "out";
  };
}) {
  const datapointId = v4();

  await dbConn.transaction(async (trx) => {
    // get stream id

    const streamId = (
      await trx("zz_job_stream_connectors")
        .select("stream_id")
        .where("project_id", "=", projectId)
        .andWhere("job_id", "=", datapoint.job_id)
        .andWhere("job_output_key", "=", datapoint.job_output_key)
        .andWhere("connector_type", "=", datapoint.connector_type)
        .first()
    )?.stream_id;

    if (!streamId) {
      throw new Error(
        `Stream not found for ${projectId}/${datapoint.job_output_key}`
      );
    }

    await dbConn<
      ZZDatapointRec<
        | T
        | {
            __primitive__: T;
          }
      >
    >("zz_datapoints").insert({
      project_id: projectId,
      stream_id: streamId,
      datapoint_id: datapointId,
      data: handlePrimitive(datapoint.data),
      job_id: datapoint.job_id,
      job_output_key: datapoint.job_output_key,
      connector_type: datapoint.connector_type,
      time_created: new Date(),
    });
  });

  return { datapointId };
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
  await dbConn.transaction(async (trx) => {
    await trx("zz_jobs")
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
      dbConn: trx,
      jobStatus: "requested",
    });
  });
}
