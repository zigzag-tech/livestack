import fs from "fs";

export async function ensurePathExists(dirPath: string): Promise<void> {
  try {
    await fs.promises.access(dirPath);
  } catch (err) {
    await fs.promises.mkdir(dirPath, { recursive: true });
  }
}
