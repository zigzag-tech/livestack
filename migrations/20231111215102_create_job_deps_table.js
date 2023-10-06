/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  // create dependency table with parent and child job ids referencing the jobs_log table
  return knex.schema.createTable("job_deps", (table) => {
    table.primary(["project_id", "parent_job_id", "child_job_id"]);
    table.string("project_id");
    table.string("parent_job_id").notNullable();
    table.string("child_job_id").notNullable();
    table.foreign("parent_job_id").references("jobs_log.job_id");
    table.foreign("child_job_id").references("jobs_log.job_id");
    table.unique(["project_id", "parent_job_id", "child_job_id"]);
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  return knex.schema.dropTable("job_deps");
};
