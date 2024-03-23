import { Knex } from "knex";
import { v4 } from "uuid";
import {
  ARRAY_KEY,
  PRIMTIVE_KEY,
  convertMaybePrimtiveOrArrayBack,
  handlePrimitiveOrArray,
} from "./primitives";
import {
  DBServiceImplementation,
  JobRec,
  EnsureStreamRecRequest,
  Order,
  ConnectorType,
} from "@livestack/vault-interface";
import _ from "lodash";
import { ensureJobRelationRec, getParentJobRec } from "./job_relations";
import {
  ZZJobStreamConnectorRec,
  ensureJobStreamConnectorRec,
} from "./streams";

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

export const dbService = (dbConn: Knex): DBServiceImplementation => ({
  async getJobRec({ projectId, specName, jobId }) {
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
      return {
        null_response: {},
      };
    }

    const withJobParams = {
      ...r,
      job_params: convertMaybePrimtiveOrArrayBack(r.job_params),
    };

    const { status, ...rest } = withJobParams;
    return {
      rec: {
        status,
        rec: rest,
      },
    };
  },
  ensureStreamRec: async (rec) => {
    return await ensureStreamRec(dbConn, rec);
  },
  ensureJobAndStatusAndConnectorRecs: async (rec, ctx) => {
    // console.log(ctx.metadata.get("auth"));

    const {
      projectId,
      specName,
      jobId,
      parentJobId,
      uniqueSpecLabel,
      jobOptionsStr,
      inputStreamIdOverridesByTag,
      outputStreamIdOverridesByTag,
    } = rec;
    const jobOptions = JSON.parse(jobOptionsStr);
    await dbConn.transaction(async (trx) => {
      await ensureJobAndInitStatusRec({
        projectId,
        specName,
        jobId,
        dbConn: trx,
        jobOptions,
      });

      if (parentJobId) {
        await ensureJobRelationRec({
          projectId: projectId,
          parentJobId: parentJobId,
          childJobId: jobId,
          dbConn: trx,
          uniqueSpecLabel: uniqueSpecLabel,
        });
      }

      if (inputStreamIdOverridesByTag) {
        for (const [key, streamId] of _.entries(inputStreamIdOverridesByTag)) {
          await ensureStreamRec(dbConn, {
            project_id: projectId,
            stream_id: streamId as string,
          });
          await ensureJobStreamConnectorRec({
            projectId,
            streamId: streamId as string,
            jobId,
            key,
            connectorType: "in",
            dbConn: trx,
          });
        }
      }

      if (outputStreamIdOverridesByTag) {
        for (const [key, streamId] of _.entries(outputStreamIdOverridesByTag)) {
          await ensureStreamRec(dbConn, {
            project_id: projectId,
            stream_id: streamId as string,
          });
          await ensureJobStreamConnectorRec({
            projectId,
            streamId: streamId as string,
            jobId,
            key,
            connectorType: "out",
            dbConn: trx,
          });
        }
      }
      await trx.commit();
    });
    return {};
  },
  updateJobInstantiatedGraph: async ({
    projectId,
    jobId,
    instantiatedGraphStr,
  }) => {
    await dbConn("zz_jobs")
      .where({
        project_id: projectId,
        job_id: jobId,
      })
      .update({
        instagraph_str: instantiatedGraphStr,
      });
    return {};
  },
  getJobDatapoints: async (
    { projectId, jobId, specName, ioType, key, order, limit },
    ctx
  ) => {
    const r = await dbConn("zz_datapoints")
      .where("zz_datapoints.project_id", "=", projectId)
      .andWhere("zz_datapoints.job_id", "=", jobId)
      .andWhere("zz_datapoints.job_output_key", "=", key)
      .orderBy(
        "zz_datapoints.time_created",
        order === Order.DESC ? "desc" : "asc"
      )
      .limit(limit || 1000)
      .select<
        ZZDatapointRec<
          | any
          | {
              [PRIMTIVE_KEY]: any;
            }
          | {
              [ARRAY_KEY]: any;
            }
        >[]
      >("*");
    // if (r.length === 0) {
    //   console.error("Job datapoints not found", jId, ioType);
    //   throw new Error(`Job datapoint for ${jId.jobId} not found!`);
    // }
    return {
      points: r.map((rec) => ({
        datapointId: rec.datapoint_id,
        dataStr: JSON.stringify(convertMaybePrimtiveOrArrayBack(rec.data)),
      })),
    };
  },
  addDatapoint: async ({
    projectId,
    streamId,
    datapointId,
    jobInfo,
    dataStr,
  }) => {
    await ensureStreamRec(dbConn, {
      project_id: projectId,
      stream_id: streamId,
    });
    const data = JSON.parse(dataStr);
    await dbConn<
      ZZDatapointRec<
        | any
        | {
            [PRIMTIVE_KEY]: any;
          }
        | {
            [ARRAY_KEY]: any;
          }
      >
    >("zz_datapoints")
      .insert({
        project_id: projectId,
        stream_id: streamId,
        datapoint_id: datapointId,
        data: handlePrimitiveOrArray(data),
        job_id: jobInfo?.jobId || null,
        job_output_key: jobInfo?.outputTag || null,
        connector_type: jobInfo ? "out" : null,
        time_created: new Date(),
      })
      .onConflict(["project_id", "stream_id", "datapoint_id"])
      .ignore();

    return { datapointId };
  },
  getJobStreamConnectorRecs: async ({
    projectId,
    jobId,
    key,
    connectorType,
  }) => {
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
    const r = (await q.select("*")).map((rec) => ({
      ...rec,
      key: rec.key,
      connector_type:
        rec.connector_type === "in" ? ConnectorType.IN : ConnectorType.OUT,
    }));
    return { records: r };
  },
  appendJobStatusRec: async ({ projectId, specName, jobId, jobStatus }) => {
    // console.debug("appendJobStatusRec", specName, jobId, jobStatus);
    await appendJobStatusRec({
      projectId,
      specName,
      jobId,
      dbConn,
      jobStatus: jobStatus as ZZJobStatus,
    });
    // console.debug("appendJobStatusRec done", specName, jobId, jobStatus);

    return {};
  },
  getParentJobRec: async ({ projectId, childJobId }) => {
    const r = await getParentJobRec({
      projectId,
      childJobId,
      dbConn,
    });
    if (!r) {
      return { null_response: {} };
    } else {
      return {
        rec: {
          ...r,
          unique_spec_label: r.unique_spec_label
            ? r.unique_spec_label
            : undefined,
          job_params:
            r.job_params ?? convertMaybePrimtiveOrArrayBack(r.job_params),
        },
      };
    }
  },
});

async function ensureStreamRec(
  dbConn: Knex,
  rec: { project_id: string; stream_id: string }
) {
  const { project_id, stream_id } = rec;
  await dbConn<
    EnsureStreamRecRequest & {
      time_created: Date;
    }
  >("zz_streams")
    .insert({
      project_id,
      stream_id,
      time_created: new Date(),
    })
    .onConflict(["project_id", "stream_id"])
    .ignore();
  return { null_response: {} };
}

async function appendJobStatusRec({
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
  instantGraphStr,
}: JobUniqueId & {
  dbConn: Knex;
  jobOptions: T;
  instantGraphStr?: string | null;
}) {
  // await dbConn.transaction(async (trx) => {
  const q = dbConn("zz_jobs")
    .insert<JobRec>({
      project_id: projectId,
      spec_name: specName,
      job_id: jobId,
      job_params: handlePrimitiveOrArray(jobOptions),
      instagraph_str: instantGraphStr,
    })
    .onConflict(["project_id", "spec_name", "job_id"])
    .ignore();

  // console.log(q.toString());
  await q;

  await appendJobStatusRec({
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
