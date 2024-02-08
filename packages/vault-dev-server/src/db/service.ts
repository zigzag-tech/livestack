import { Knex } from "knex";
import { v4 } from "uuid";
import {
  convertMaybePrimtiveOrArrayBack,
  handlePrimitiveOrArray,
} from "./primitives";
import {
  DBServiceImplementation,
  JobRec,
  EnsureStreamRecRequest,
  ConnectorType,
} from "@livestack/vault-interface";
import _ from "lodash";
import { dbClient } from "@livestack/vault-client";
import { ensureJobRelationRec } from "./job_relations";
import { ensureJobStreamConnectorRec } from "./streams";

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
  },
  ensureJobAndStatusAndConnectorRecs: async (rec) => {
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
          await dbClient.ensureStreamRec({
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
          dbClient;
          await dbClient.ensureStreamRec({
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
