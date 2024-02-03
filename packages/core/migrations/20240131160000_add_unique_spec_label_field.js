/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  // add unique_spec_label field to zz_job_relations table
  // and change the primary key to be a combination of
  // project_id", "parent_job_id", "child_job_id", "unique_spec_label"
  return knex.schema.alterTable("zz_job_relations", (table) => {
    table.string("unique_spec_label").notNullable().defaultTo("null");
    table.dropPrimary();
    table.primary([
      "project_id",
      "parent_job_id",
      "child_job_id",
      "unique_spec_label",
    ]);
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  // remove unique_spec_label field from zz_job_relations table
  // and change the primary key to be a combination of
  // project_id", "parent_job_id", "child_job_id"
  return knex.schema.alterTable("zz_job_relations", (table) => {
    table.dropPrimary();
    table.primary(["project_id", "parent_job_id", "child_job_id"]);
    table.dropColumn("unique_spec_label");
  });
};
