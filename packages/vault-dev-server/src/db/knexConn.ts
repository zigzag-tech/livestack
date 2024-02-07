import { getDatabaseInstance } from "./db_funcs";

if (
  !(
    process.env.DB_HOST &&
    process.env.DB_PORT &&
    process.env.DB_USER &&
    process.env.DB_PWD &&
    process.env.DB_NAME
  )
) {
  throw new Error(
    "One or more among DB_HOST, DB_PORT, DB_USER, DB_PWD, DB_NAME is not defined"
  );
}

export const db = getDatabaseInstance({
  host: process.env.DB_HOST,
  port: process.env.DB_PORT,
  user: process.env.DB_USER,
  password: process.env.DB_PWD,
  database: process.env.DB_NAME,
});
