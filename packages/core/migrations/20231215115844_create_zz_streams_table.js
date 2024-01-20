/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.up = function (knex) {
  return knex.schema.createTable("zz_streams", function (table) {
    table.primary(["project_id", "stream_id"]);
    table.string("project_id").notNullable();
    table.string("stream_id").notNullable();
    table.timestamp("time_created").defaultTo(knex.fn.now());
  });
};

/**
 * @param { import("knex").Knex } knex
 * @returns { Promise<void> }
 */
exports.down = function (knex) {
  return knex.schema.dropTable("zz_streams");
};
