/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  return knex.schema.createTable("zz_job_status", function (table) {
    table.primary(["status_id"]);
    table.string("status_id").notNullable();
    table.string("project_id").notNullable();
    table.string("op_name").notNullable();
    table.string("job_id").notNullable();
    table.string("status").notNullable();
    table.timestamp("time_created").defaultTo(knex.fn.now());

    // add foreign key for primary key
    table
      .foreign(["project_id", "op_name", "job_id"])
      .references(["project_id", "op_name", "job_id"])
      .inTable("zz_jobs");

    // add index for primary key
    table.index(["project_id", "op_name", "job_id"]);
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  return knex.schema.dropTable("zz_job_status");
};
