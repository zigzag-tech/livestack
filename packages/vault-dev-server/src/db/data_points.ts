import { Knex } from "knex";
import {
  ARRAY_KEY,
  PRIMTIVE_KEY,
  convertMaybePrimtiveOrArrayBack,
  handlePrimitiveOrArray,
} from "./primitives";
import { JobUniqueId } from "./jobs";

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
          [PRIMTIVE_KEY]: T;
        }
      | {
          [ARRAY_KEY]: T;
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
    data: convertMaybePrimtiveOrArrayBack(rec.data),
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
    outputTag: string;
  };
  data: T;
}) {
  await dbConn<
    ZZDatapointRec<
      | T
      | {
          [PRIMTIVE_KEY]: T;
        }
      | {
          [ARRAY_KEY]: T;
        }
    >
  >("zz_datapoints").insert({
    project_id: projectId,
    stream_id: streamId,
    datapoint_id: datapointId,
    data: handlePrimitiveOrArray(data),
    job_id: jobInfo?.jobId || null,
    job_output_key: jobInfo?.outputTag || null,
    connector_type: jobInfo ? "out" : null,
    time_created: new Date(),
  });

  return { datapointId };
}
export interface ZZDatapointRec<T> {
  project_id: string;
  stream_id: string;
  datapoint_id: string;
  data: T;
  job_id: string | null;
  job_output_key: string | null;
  connector_type: "out" | null;
  time_created: Date;
}
