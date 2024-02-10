import { getDatabaseInstance } from "./db_funcs";

export const db = getDatabaseInstance({
  dbPath: process.env.DB_PATH,
});
