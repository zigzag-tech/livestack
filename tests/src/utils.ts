import fs from "fs";

export function sleep(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

export function saveToJSON(g:  { toJson: () => string }) {
  fs.writeFileSync("graph.json", JSON.stringify(JSON.parse(g.toJson()), null, 2));
}

export async function runAtLeast<T>(ms: number, p: Promise<T>): Promise<T> {
  await Promise.race([p, sleep(ms)]);
  return await p;
}
