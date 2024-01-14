import {
  ZZDatapointRec,
  ZZJobStreamConnectorRec,
  ZZStreamRec,
} from "./rec_types";
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
  specName: string;
  jobId: string;
};

export async function getJobRec<T>({
  projectId,
  specName,
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
      this.on("zz_jobs.spec_name", "=", "zz_job_status.spec_name");
    })
    .where("zz_jobs.project_id", "=", projectId)
    .andWhere("zz_jobs.spec_name", "=", specName)
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

export async function ensureStreamRec({
  projectId,
  streamId,
  dbConn,
}: {
  projectId: string;
  dbConn: Knex;
  streamId: string;
}) {
  await dbConn<ZZStreamRec>("zz_streams")
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
    .where("zz_datapoints.project_id", "=", jId.projectId)
    .andWhere("zz_datapoints.job_id", "=", jId.jobId)
    .andWhere("zz_datapoints.job_output_key", "=", key)
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
  streamId,
  data,
  jobInfo,
  datapointId,
}: {
  datapointId: string;
  dbConn: Knex;
  projectId: string;
  streamId: string;
  jobInfo?: {
    jobId: string;
    jobOutputKey: string;
  };
  data: T;
}) {
  // console.debug(
  //   "addDatapoint",
  //   JSON.stringify(
  //     {
  //       projectId,
  //       streamId,
  //       jobInfo,
  //       data,
  //       datapointId,
  //     },
  //     longStringTruncator
  //   )
  // );
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
    data: handlePrimitive(data),
    job_id: jobInfo?.jobId || null,
    job_output_key: jobInfo?.jobOutputKey || null,
    connector_type: jobInfo ? "out" : null,
    time_created: new Date(),
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
  jobParams,
}: JobUniqueId & {
  dbConn: Knex;
  jobParams: T;
}) {
  // await dbConn.transaction(async (trx) => {
  const q = dbConn("zz_jobs")
    .insert<ZZJobRec<T>>({
      project_id: projectId,
      spec_name: specName,
      job_id: jobId,
      job_params: handlePrimitive(jobParams),
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
