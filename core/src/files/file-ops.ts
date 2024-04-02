import { IStorageProvider } from "../storage/cloudStorage";
import { detectBinaryLikeObject } from "../utils/isBinaryLikeObject";
import { TEMP_DIR } from "../storage/temp-dirs";
import fs from "fs";
import { join as pathJoin } from "path";
import { ensurePathExists } from "../storage/ensurePathExists";
import path from "path";
import _, { isUndefined } from "lodash";
const OBJ_REF_VALUE = `__zz_obj_ref__`;
import { Readable } from "stream";
const LARGE_VALUE_THRESHOLD = 1024 * 1024 * 2;

export type OriginalType =
  | Exclude<ReturnType<typeof detectBinaryLikeObject>, false>
  | "string";

type LargeFileToSave<T extends OriginalType> = {
  path: string;
  value: any;
  originalType: T;
};

export const identifyLargeFilesToSave = (
  obj: any,
  path = ""
): {
  newObj: any;
  largeFilesToSave: LargeFileToSave<any>[];
} => {
  if (obj === null || isUndefined(obj)) {
    return { newObj: obj, largeFilesToSave: [] };
  }
  const largeFilesToSave: LargeFileToSave<any>[] = [];
  if (typeof obj === "string" && obj.length > LARGE_VALUE_THRESHOLD) {
    largeFilesToSave.push({
      path,
      value: obj,
      originalType: "string",
    });
    return {
      newObj: {
        [OBJ_REF_VALUE]: true,
        originalType: "string",
      },
      largeFilesToSave,
    };
  } else {
    const type = detectBinaryLikeObject(obj);
    if (type) {
      largeFilesToSave.push({ path, value: obj, originalType: type });
      return {
        newObj: {
          [OBJ_REF_VALUE]: true,
          originalType: "string",
        },
        largeFilesToSave,
      };
    }
  }
  let newObj: any = Array.isArray(obj) ? [] : {};
  if (typeof obj === "object") {
    for (const [key, value] of _.entries(obj)) {
      const currentPath = path ? pathJoin(path, key) : key;
      const result = identifyLargeFilesToSave(value, currentPath);
      newObj[key] = result.newObj;
      largeFilesToSave.push(...result.largeFilesToSave);
    }
    return { newObj, largeFilesToSave };
  } else {
    return { newObj: obj, largeFilesToSave };
  }
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
  if (obj === null || isUndefined(obj)) {
    return {
      largeFilesToRestore: [],
      newObj: obj,
    };
  }
  const largeFilesToRestore: LargeFileWithoutValue<any>[] = [];
  if (typeof obj === "object" && obj[OBJ_REF_VALUE] && obj.originalType) {
    largeFilesToRestore.push({
      originalType: obj.originalType,
      path: path,
    });
    return { largeFilesToRestore, newObj: obj };
  }
  const newObj: any = Array.isArray(obj) ? [] : {};
  if (typeof obj === "object") {
    for (const [key, value] of _.entries(obj)) {
      const currentPath = path ? pathJoin(path, key) : key;
      const result = identifyLargeFilesToRestore(value, currentPath);
      newObj[key] = result.newObj;
      largeFilesToRestore.push(...result.largeFilesToRestore);
    }
    return { largeFilesToRestore, newObj };
  } else {
    return { largeFilesToRestore, newObj: obj };
  }
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

export async function restoreLargeValues({
  obj_,
  largeFilesToRestore,
  basePath = "",
  fetcher,
}: {
  obj_: any;
  basePath?: string;
  largeFilesToRestore: { path: string; originalType: OriginalType }[];
  fetcher: <T extends OriginalType>(
    v: LargeFileWithoutValue<T>
  ) => Promise<InferRestoredFileType<T>>;
}) {
  // iterate over the large files to restore, and replace the value in the path
  // with the value returned from the fetcher.
  // use p-limit to limit the number of concurrent fetches
  const limit = pLimit(3);
  const promises = largeFilesToRestore.map((largeFile) =>
    limit(async () => {
      const value = await fetcher({
        ...largeFile,
        path: path.join(basePath, largeFile.path),
      });
      // replace slash with dot in path before setting the value
      const p = largeFile.path.replace(/\//g, ".");
      if (p === "") {
        obj_ = value;
      } else {
        _.set(obj_, p, value);
      }
    })
  );
  await Promise.all(promises);
  return obj_;
}

// export async function ensureLocalSourceFileExists(
//   storageProvider: IStorageProvider,
//   filePath: string
// ) {
//   try {
//     fs.accessSync(filePath);
//   } catch (error) {
//     if (storageProvider) {
//       ensurePathExists(filePath);
//       const gcsFileName = filePath.split(`${TEMP_DIR}/`)[1].replace(/_/g, "/");
//       await storageProvider.downloadFromStorage({
//         filePath: gcsFileName,
//         destination: filePath,
//       });
//     }
//   }
// }
