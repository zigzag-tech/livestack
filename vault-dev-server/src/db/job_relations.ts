import { Knex } from "knex";
import { convertMaybePrimtiveOrArrayBack } from "./primitives";
import { JobRec } from "@livestack/vault-interface";

export async function ensureJobRelationRec({
  projectUuid,
  parentJobId,
  childJobId,
  dbConn,
  uniqueSpecLabel,
}: {
  projectUuid: string;
  parentJobId: string;
  childJobId: string;
  dbConn: Knex;
  uniqueSpecLabel?: string;
}) {
  await dbConn("zz_job_relations")
    .insert<LiveJobRelationRec>({
      project_uuid: projectUuid,
      parent_job_id: parentJobId,
      child_job_id: childJobId,
      time_created: new Date(),
      unique_spec_label: uniqueSpecLabel || "null",
    })
    .onConflict([
      "project_uuid",
      "parent_job_id",
      "child_job_id",
      "unique_spec_label",
    ])
    .merge();
}

// export async function getChildJobs({
//   projectUuid,
//   parentJobId,
//   dbConn,
// }: {
//   projectUuid: string;
//   parentJobId: string;
//   dbConn: Knex;
// }) {
//   // join the job table to get the job spec name and params
//   const r = await dbConn("zz_job_relations")
//     .select<
//       (JobRec & {
//         unique_spec_label: string | null;
//       })[]
//     >(["zz_jobs.*", "zz_job_relations.unique_spec_label"])
//     .leftJoin("zz_jobs", function () {
//       this.on("zz_job_relations.child_job_id", "=", "zz_jobs.job_id");
//       this.on("zz_job_relations.project_uuid", "=", "zz_jobs.project_uuid");
//     })
//     .where("zz_job_relations.project_uuid", "=", projectUuid)
//     .andWhere("zz_job_relations.parent_job_id", "=", parentJobId);
//   return r.map((rec) => ({
//     ...rec,
//     job_params: convertMaybePrimtiveOrArrayBack(rec.job_params),
//     unique_spec_label:
//       rec.unique_spec_label === "null" ? null : rec.unique_spec_label,
//   }));
// }

export async function getParentJobRec({
  projectUuid,
  childJobId,
  dbConn,
}: {
  projectUuid: string;
  childJobId: string;
  dbConn: Knex;
}) {
  // join the job table to get the job spec name and params
  const rec = await dbConn("zz_job_relations")
    .first<
      JobRec & {
        unique_spec_label: string | null;
      }
    >([
      "zz_jobs.*",
      "zz_job_relations.unique_spec_label",
      "zz_job_relations.parent_job_id",
    ])
    .leftJoin("zz_jobs", function () {
      this.on("zz_job_relations.parent_job_id", "=", "zz_jobs.job_id");
      this.on("zz_job_relations.project_uuid", "=", "zz_jobs.project_uuid");
    })
    .where("zz_job_relations.project_uuid", "=", projectUuid)
    .andWhere("zz_job_relations.child_job_id", "=", childJobId);
  if (!rec) {
    return null;
  } else {
    return {
      ...rec,
      job_params: convertMaybePrimtiveOrArrayBack(rec.job_params),
      unique_spec_label:
        rec.unique_spec_label === "null" ? null : rec.unique_spec_label,
    };
  }
}
export interface LiveJobRelationRec {
  project_uuid: string;
  parent_job_id: string;
  child_job_id: string;
  time_created: Date;
}
