import { IStorageProvider } from "../storage/cloudStorage";
import { isBinaryLikeObject } from "../utils/isBinaryLikeObject";
import { TEMP_DIR } from "../storage/temp-dirs";
import fs from "fs";
import { ensurePathExists } from "../storage/ensurePathExists";
import _ from "lodash";
const OBJ_REF_VALUE = `__zz_obj_ref__`;
const LARGE_VALUE_THRESHOLD = 1024 * 10;

export const identifyLargeFiles = (
  obj: any,
  path = ""
): { newObj: any; largeFilesToSave: { path: string; value: any }[] } => {
  if (obj === null || typeof obj !== "object") {
    return { newObj: obj, largeFilesToSave: [] };
  }
  const newObj: any = Array.isArray(obj) ? [] : {};
  const largeFilesToSave: { path: string; value: any }[] = [];

  for (const [key, value] of _.entries(obj)) {
    const currentPath = path ? `${path}/${key}` : key;

    if (typeof value === "string" && value.length > LARGE_VALUE_THRESHOLD) {
      largeFilesToSave.push({ path: currentPath, value });
      newObj[key] = OBJ_REF_VALUE;
    } else if (isBinaryLikeObject(value)) {
      largeFilesToSave.push({ path: currentPath, value });
      newObj[key] = OBJ_REF_VALUE;
    } else if (typeof value === "object") {
      const result = identifyLargeFiles(value, currentPath);
      newObj[key] = result.newObj;
      largeFilesToSave.push(...result.largeFilesToSave);
    } else {
      newObj[key] = value;
    }
  }
  return { newObj, largeFilesToSave };
};

export async function ensureLocalSourceFileExists(
  storageProvider: IStorageProvider,
  filePath: string
) {
  try {
    fs.accessSync(filePath);
  } catch (error) {
    if (storageProvider) {
      ensurePathExists(filePath);
      const gcsFileName = filePath.split(`${TEMP_DIR}/`)[1].replace(/_/g, "/");
      await storageProvider.downloadFromStorage({
        filePath: gcsFileName,
        destination: filePath,
      });
    }
  }
}
