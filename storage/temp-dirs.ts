import os from "os";
import path from "path";

export const TEMP_DIR = os.tmpdir();

export function getTempPathByJobId(jobId: string) {
  return path.join(TEMP_DIR, jobId);
}
