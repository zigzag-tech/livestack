import { Knex } from "knex";
import { convertMaybePrimtiveOrArrayBack } from "./primitives";
import { JobRec } from "@livestack/vault-interface";

export async function ensureJobRelationRec({
  projectId,
  parentJobId,
  childJobId,
  dbConn,
  uniqueSpecLabel,
}: {
  projectId: string;
  parentJobId: string;
  childJobId: string;
  dbConn: Knex;
  uniqueSpecLabel?: string;
}) {
  await dbConn("zz_job_relations")
    .insert<ZZJobRelationRec>({
      project_id: projectId,
      parent_job_id: parentJobId,
      child_job_id: childJobId,
      time_created: new Date(),
      unique_spec_label: uniqueSpecLabel || "null",
    })
    .onConflict([
      "project_id",
      "parent_job_id",
      "child_job_id",
      "unique_spec_label",
    ])
    .merge();
}

// export async function getChildJobs({
//   projectId,
//   parentJobId,
//   dbConn,
// }: {
//   projectId: string;
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
//       this.on("zz_job_relations.project_id", "=", "zz_jobs.project_id");
//     })
//     .where("zz_job_relations.project_id", "=", projectId)
//     .andWhere("zz_job_relations.parent_job_id", "=", parentJobId);
//   return r.map((rec) => ({
//     ...rec,
//     job_params: convertMaybePrimtiveOrArrayBack(rec.job_params),
//     unique_spec_label:
//       rec.unique_spec_label === "null" ? null : rec.unique_spec_label,
//   }));
// }

export async function getParentJobRec({
  projectId,
  childJobId,
  dbConn,
}: {
  projectId: string;
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
      this.on("zz_job_relations.project_id", "=", "zz_jobs.project_id");
    })
    .where("zz_job_relations.project_id", "=", projectId)
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
export interface ZZJobRelationRec {
  project_id: string;
  parent_job_id: string;
  child_job_id: string;
  time_created: Date;
}
