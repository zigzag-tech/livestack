/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  return knex.schema.createTable("zz_job_data", function (table) {
    table.primary(["job_data_id"]);
    table.string("job_data_id").notNullable();
    table.jsonb("job_data").notNullable();
    table.timestamp("time_created").defaultTo(knex.fn.now());
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  return knex.schema.dropTable("zz_job_data");
};
