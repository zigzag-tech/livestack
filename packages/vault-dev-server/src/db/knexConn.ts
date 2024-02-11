import { getSQLite3DatabaseInstance } from "./db_funcs";

export const db = getSQLite3DatabaseInstance({
  dbPath: process.env.DB_PATH,
});
