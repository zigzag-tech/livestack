/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  return knex.schema.createTable("zz_job_stream_connectors", function (table) {
    table.primary(["project_id", "job_id", "key", "connector_type"]);
    table.string("project_id").notNullable();
    table.string("job_id").notNullable();
    table.string("stream_id").notNullable();
    table.string("key").notNullable();
    table.string("connector_type").notNullable(); // "input" or "output"
    table.timestamp("time_created").defaultTo(knex.fn.now());

    // add foreign key for parent job
    table
      .foreign(["project_id", "job_id"])
      .references(["project_id", "job_id"])
      .inTable("zz_jobs");

    table
      .foreign(["project_id", "stream_id"])
      .references(["project_id", "stream_id"])
      .inTable("zz_streams");

    // add index for primary key
    table.index(["project_id", "job_id", "stream_id", "key", "connector_type"]);
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  return knex.schema.dropTable("zz_job_stream_connectors");
};
