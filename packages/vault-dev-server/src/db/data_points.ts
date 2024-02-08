import { Knex } from "knex";
import {
  ARRAY_KEY,
  PRIMTIVE_KEY,
  convertMaybePrimtiveOrArrayBack,
  handlePrimitiveOrArray,
} from "./primitives";
import { JobUniqueId } from "./service";

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
