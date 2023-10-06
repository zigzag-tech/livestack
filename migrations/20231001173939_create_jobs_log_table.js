/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  return knex.schema.createTable("jobs_log", function (table) {
    table.primary(["project_id", "job_type", "job_id"]);
    table.string("project_id").notNullable();
    table.string("job_type").notNullable();
    table.string("job_id").notNullable();
    table.jsonb("job_data");
    table.timestamp("time_created").defaultTo(knex.fn.now());
    table.timestamp("time_updated").defaultTo(knex.fn.now());
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  return knex.schema.dropTable("jobs_log");
};
