import { Knex } from "knex";

export async function ensureTables(knex: Knex): Promise<void> {
  if (!(await knex.schema.hasTable("zz_streams"))) {
    await knex.schema.createTable("zz_streams", (table) => {
      table.string("project_uuid").notNullable();
      table.string("stream_id").notNullable();
      table.timestamp("time_created").defaultTo(knex.fn.now());
      table.primary(["project_uuid", "stream_id"]);
    });
  }

  if (!(await knex.schema.hasTable("zz_jobs"))) {
    await knex.schema.createTable("zz_jobs", (table) => {
      table.string("project_uuid").notNullable();
      table.string("spec_name").notNullable();
      table.string("job_id").notNullable();
      table.json("job_params");
      table.timestamp("time_created").defaultTo(knex.fn.now());
      table.primary(["project_uuid", "spec_name", "job_id"]);
      table.unique(["project_uuid", "job_id"]);
    });
  }

  if (!(await knex.schema.hasTable("zz_job_status"))) {
    await knex.schema.createTable("zz_job_status", (table) => {
      table.string("status_id").notNullable();
      table.string("project_uuid").notNullable();
      table.string("spec_name").notNullable();
      table.string("job_id").notNullable();
      table.string("status").notNullable();
      table.timestamp("time_created").defaultTo(knex.fn.now());
      table.primary(["status_id"]);
      table
        .foreign(["project_uuid", "spec_name", "job_id"])
        .references(["project_uuid", "spec_name", "job_id"])
        .inTable("zz_jobs");
    });
  }

  if (!(await knex.schema.hasTable("zz_datapoints"))) {
    await knex.schema.createTable("zz_datapoints", (table) => {
      table.string("project_uuid").notNullable();
      table.string("stream_id").notNullable();
      table.string("datapoint_id").notNullable();
      table.json("data").notNullable();
      table.string("job_id");
      table.string("job_output_key");
      table.string("connector_type").nullable();
      table.check("connector_type = 'out' OR connector_type IS NULL");
      table.timestamp("time_created").defaultTo(knex.fn.now());
      table.primary(["project_uuid", "stream_id", "datapoint_id"]);
      table
        .foreign(["project_uuid", "stream_id"])
        .references(["project_uuid", "stream_id"])
        .inTable("zz_streams");
      table
        .foreign(["project_uuid", "job_id"])
        .references(["project_uuid", "job_id"])
        .inTable("zz_jobs");
    });
  }

  if (!(await knex.schema.hasTable("zz_job_stream_connectors"))) {
    await knex.schema.createTable("zz_job_stream_connectors", (table) => {
      table.string("project_uuid").notNullable();
      table.string("job_id").notNullable();
      table.string("key").notNullable();
      table.string("connector_type").notNullable();
      table.string("stream_id").notNullable();
      table.timestamp("time_created").defaultTo(knex.fn.now());
      table.primary(["project_uuid", "job_id", "key", "connector_type"]);
      table
        .foreign(["project_uuid", "job_id"])
        .references(["project_uuid", "job_id"])
        .inTable("zz_jobs");
      table
        .foreign(["project_uuid", "stream_id"])
        .references(["project_uuid", "stream_id"])
        .inTable("zz_streams");
    });
  }

  if (!(await knex.schema.hasTable("zz_job_relations"))) {
    await knex.schema.createTable("zz_job_relations", (table) => {
      table.string("project_uuid").notNullable();
      table.string("parent_job_id").notNullable();
      table.string("child_job_id").notNullable();
      table.string("unique_spec_label").notNullable().defaultTo("null");
      table.timestamp("time_created").defaultTo(knex.fn.now());
      table.primary([
        "project_uuid",
        "parent_job_id",
        "child_job_id",
        "unique_spec_label",
      ]);
      table
        .foreign(["project_uuid", "parent_job_id"])
        .references(["project_uuid", "job_id"])
        .inTable("zz_jobs");
      table
        .foreign(["project_uuid", "child_job_id"])
        .references(["project_uuid", "job_id"])
        .inTable("zz_jobs");
    });
  }
  console.info("Tables created.");
}
