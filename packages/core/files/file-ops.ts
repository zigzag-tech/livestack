import { IStorageProvider } from "../storage/cloudStorage";
import { detectBinaryLikeObject } from "../utils/isBinaryLikeObject";
import { TEMP_DIR } from "../storage/temp-dirs";
import fs, { join as pathJoin } from "fs";
import { ensurePathExists } from "../storage/ensurePathExists";
import _ from "lodash";
const OBJ_REF_VALUE = `__zz_obj_ref__`;
import { Readable } from "stream";
const LARGE_VALUE_THRESHOLD = 1024 * 10;

export type OriginalType =
  | Exclude<ReturnType<typeof detectBinaryLikeObject>, false>
  | "string";

type LargeFileToSave<T extends OriginalType> = {
  path: string;
  value: any;
  originalType: T;
};

export const identifyLargeFiles = (
  obj: any,
  path = ""
): {
  newObj: any;
  largeFilesToSave: LargeFileToSave<any>[];
} => {
  if (obj === null || typeof obj !== "object") {
    return { newObj: obj, largeFilesToSave: [] };
  }
  const newObj: any = Array.isArray(obj) ? [] : {};
  const largeFilesToSave: LargeFileToSave<any>[] = [];

  for (const [key, value] of _.entries(obj)) {
    const currentPath = path ? pathJoin(path, key) : key;

    if (typeof value === "string" && value.length > LARGE_VALUE_THRESHOLD) {
      largeFilesToSave.push({
        path: currentPath,
        value,
        originalType: "string",
      });
      newObj[key] = {
        [OBJ_REF_VALUE]: true,
        originalType: "string",
      };
    } else {
      const type = detectBinaryLikeObject(value);
      if (type) {
        largeFilesToSave.push({ path: currentPath, value, originalType: type });
        newObj[key] = {
          [OBJ_REF_VALUE]: true,
          originalType: type,
        };
      } else if (typeof value === "object") {
        const result = identifyLargeFiles(value, currentPath);
        newObj[key] = result.newObj;
        largeFilesToSave.push(...result.largeFilesToSave);
      } else {
        newObj[key] = value;
      }
    }
  }
  return { newObj, largeFilesToSave };
};

type LargeFileWithoutPath<T extends OriginalType> = Omit<
  LargeFileToSave<T>,
  "path"
>;

export function identifyLargeFilesToRestore(
  obj: any,
  path = ""
): {
  largeFilesToRestore: LargeFileWithoutValue<any>[];
  newObj: any;
} {
  if (obj === null || typeof obj !== "object") {
    return {
      largeFilesToRestore: [],
      newObj: obj,
    };
  }
  const largeFilesToRestore: LargeFileWithoutValue<any>[] = [];
  const newObj: any = Array.isArray(obj) ? [] : {};
  for (const [key, value] of _.entries(obj)) {
    const currentPath = path ? pathJoin(path, key) : key;
    if (value && (value as any)[OBJ_REF_VALUE]) {
      largeFilesToRestore.push({
        path: currentPath,
        originalType: (value as LargeFileWithoutPath<any>).originalType,
      });
    } else if (typeof value === "object") {
      const result = identifyLargeFilesToRestore(value, currentPath);
      newObj[key] = result.newObj;
      largeFilesToRestore.push(...result.largeFilesToRestore);
    } else {
      newObj[key] = value;
    }
  }

  return { largeFilesToRestore, newObj };
}

export type LargeFileWithoutValue<T extends OriginalType> = Omit<
  LargeFileToSave<T>,
  "value"
>;
import pLimit from "p-limit";

export type InferRestoredFileType<T extends OriginalType> = T extends "string"
  ? string
  : T extends "buffer"
  ? Buffer
  : T extends "stream"
  ? Readable
  : T extends "file"
  ? File
  : T extends "blob"
  ? Blob
  : T extends "arrayBuffer"
  ? ArrayBuffer
  : never;

export async function restoreLargeValues(
  obj_: any,
  largeFilesToRestore: { path: string; originalType: OriginalType }[],
  fetcher: <T extends OriginalType>(
    v: LargeFileWithoutValue<T>
  ) => Promise<InferRestoredFileType<T>>
) {
  // iterate over the large files to restore, and replace the value in the path
  // with the value returned from the fetcher.
  // use p-limit to limit the number of concurrent fetches
  const limit = pLimit(10);
  const promises = largeFilesToRestore.map((largeFile) =>
    limit(async () => {
      const value = await fetcher(largeFile);
      _.set(obj_, largeFile.path, value);
    })
  );
  await Promise.all(promises);
  return obj_;
}

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
