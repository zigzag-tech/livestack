import { sleep } from "@livestack/core";

export async function runAtLeast<T>(ms: number, p: Promise<T>): Promise<T> {
  await Promise.race([p, sleep(ms)]);
  return await p;
}
