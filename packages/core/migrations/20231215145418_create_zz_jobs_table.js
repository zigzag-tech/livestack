/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  return knex.schema.createTable("zz_jobs", function (table) {
    table.primary(["project_id", "spec_name", "job_id"]);
    table.unique(["project_id", "job_id"]);
    table.string("project_id").notNullable();
    table.string("spec_name").notNullable();
    table.string("job_id").notNullable();
    table.jsonb("job_params").nullable();
    table.timestamp("time_created").defaultTo(knex.fn.now());

    // add index for primary key
    table.index(["project_id", "spec_name", "job_id"]);
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  return knex.schema.dropTable("zz_jobs");
};
