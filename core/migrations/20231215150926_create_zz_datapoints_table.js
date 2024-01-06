/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  return knex.schema.createTable("zz_datapoints", function (table) {
    table.primary(["project_id", "stream_id", "datapoint_id"]);
    table.string("project_id").notNullable();
    table.string("stream_id").notNullable();
    table.jsonb("data").notNullable();
    table.string("datapoint_id").notNullable();
    table.string("job_id").nullable();
    table.string("job_output_key").nullable();

    // hack to make foreign key work
    table.string("connector_type").nullable();

    // add constraint for connector_type to be either out or null
    table.check("connector_type = 'out' or connector_type is null");

    table.timestamp("time_created").defaultTo(knex.fn.now());

    // add foreign key for primary key
    table
      .foreign(["project_id", "stream_id"])
      .references(["project_id", "stream_id"])
      .inTable("zz_streams");

    // add foreign key for job_data_id
    table
      .foreign(["project_id", "job_id"])
      .references(["project_id", "job_id"])
      .inTable("zz_jobs");

    // add index for primary key
    table.index(["project_id", "stream_id", "datapoint_id"]);
    table.index(["job_id", "job_output_key"]);
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  return knex.schema.dropTable("zz_datapoints");
};
