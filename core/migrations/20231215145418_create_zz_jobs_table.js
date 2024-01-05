/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  return knex.schema.createTable("zz_jobs", function (table) {
    table.primary(["project_id", "op_name", "job_id"]);
    table.string("project_id").notNullable();
    table.string("op_name").notNullable();
    table.string("job_id").notNullable();
    table.jsonb("init_params").nullable();
    table.timestamp("time_created").defaultTo(knex.fn.now());

    // add index for primary key
    table.index(["project_id", "op_name", "job_id"]);
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  return knex.schema.dropTable("zz_jobs");
};
