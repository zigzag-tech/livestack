/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  return knex.schema.createTable("zz_job_relations", function (table) {
    table.primary(["project_id", "parent_job_id", "child_job_id"]);
    table.string("project_id").notNullable();
    table.string("parent_job_id").notNullable();
    table.string("child_job_id").notNullable();
    table.timestamp("time_created").defaultTo(knex.fn.now());

    // add foreign key for parent job
    table
      .foreign(["project_id", "parent_job_id"])
      .references(["project_id", "job_id"])
      .inTable("zz_jobs");

    // add foreign key for child job
    table
      .foreign(["project_id", "child_job_id"])
      .references(["project_id", "job_id"])
      .inTable("zz_jobs");

    // add index for primary key
    table.index(["project_id", "parent_job_id", "child_job_id"]);
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  return knex.schema.dropTable("zz_job_relations");
};
