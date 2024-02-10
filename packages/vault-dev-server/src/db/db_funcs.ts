import Knex from "knex";
import { ensureTables } from "./ensureTables";

export const getDatabaseInstance = ({
  dbPath = "./livestack.sqlite",
}: {
  dbPath?: string;
}) => {
  const knex = Knex({
    client: "better-sqlite3",
    connection: {
      filename: dbPath,
    },
    useNullAsDefault: true,
  });
  ensureTables(knex);
  return knex;
};
