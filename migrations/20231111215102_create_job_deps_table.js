/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = async function (knex) {
  await knex.schema.alterTable("jobs_log", (table) => {
    table.unique("job_id");
  });
  return await knex.schema.createTable("job_deps", (table) => {
    table.primary(["project_id", "parent_job_id", "child_job_id"]);
    table.string("project_id");
    table.string("parent_job_id").notNullable();
    table.string("child_job_id").notNullable();
    table.foreign("parent_job_id").references("jobs_log.job_id");
    table.foreign("child_job_id").references("jobs_log.job_id");
    table.unique(["project_id", "parent_job_id", "child_job_id"]);
  });
};

exports.down = async function (knex) {
  await knex.schema.dropTable("job_deps");
  return await knex.schema.alterTable("jobs_log", (table) => {
    table.dropUnique("job_id");
  });
};
