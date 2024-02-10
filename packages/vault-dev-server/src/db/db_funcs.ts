import knex from "knex";

export const getDatabaseInstance = ({
  dbPath = "./livestack.sqlite",
}: {
  dbPath?: string;
}) =>
  knex({
    client: "better-sqlite3",
    connection: {
      filename: dbPath,
    },
    useNullAsDefault: true,
  });
