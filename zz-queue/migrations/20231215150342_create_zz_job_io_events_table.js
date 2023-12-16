/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  return knex.schema.createTable("zz_job_io_events", function (table) {
    table.primary(["io_event_id"]);
    table.string("io_event_id").notNullable();
    table.string("project_id").notNullable();
    table.string("op_name").notNullable();
    table.string("job_id").notNullable();
    table.string("io_type").notNullable();
    table.string("job_data_id").notNullable();
    table.string("spawn_phase_id").nullable();
    table.timestamp("time_created").defaultTo(knex.fn.now());

    // add foreign key for primary key
    table
      .foreign(["project_id", "op_name", "job_id"])
      .references(["project_id", "op_name", "job_id"])
      .inTable("zz_jobs");

    // add foreign key for job_data_id
    table
      .foreign(["job_data_id"])
      .references(["job_data_id"])
      .inTable("zz_job_data");

    // add index for primary key
    table.index(["project_id", "op_name", "job_id"]);
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  return knex.schema.dropTable("zz_job_io_events");
};
