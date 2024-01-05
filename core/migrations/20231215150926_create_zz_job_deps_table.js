/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  return knex.schema.createTable("zz_job_deps", function (table) {
    table.primary([
      "project_id",
      "parent_job_id",
      "child_job_id",
      "io_event_id",
    ]);
    table.string("project_id").notNullable();
    table.string("parent_job_id").notNullable();
    table.string("parent_pipe_name").notNullable();
    table.string("child_job_id").notNullable();
    table.string("child_pipe_name").notNullable();
    table.string("io_event_id").nullable();

    // add foreign key for parent job
    table
      .foreign(["project_id", "parent_job_id", "parent_pipe_name"])
      .references(["project_id", "job_id", "pipe_name"])
      .inTable("zz_jobs");

    // add foreign key for child job
    table
      .foreign(["project_id", "child_job_id", "child_pipe_name"])
      .references(["project_id", "job_id", "pipe_name"])
      .inTable("zz_jobs");

    // add foreign key for io event
    table
      .foreign(["io_event_id"])
      .references(["io_event_id"])
      .inTable("zz_job_io_events");

    // add index for primary key
    table.index(["project_id", "parent_job_id", "child_job_id"]);
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  return knex.schema.dropTable("zz_job_deps");
};
