import { detectBinaryLikeObject } from "../utils/isBinaryLikeObject";
import { join as pathJoin } from "path";
import path from "path";
import _, { isUndefined } from "lodash";

import { Readable } from "stream";
const LARGE_VALUE_THRESHOLD = 1024 * 32;

export type OriginalType =
  | Exclude<Awaited<ReturnType<typeof detectBinaryLikeObject>>, false>["type"]
  | "string";

type LargeFileToSave<T extends OriginalType> = {
  path: string;
  value: any;
  originalType: T;
  hash?: string;
};

export const identifyLargeFilesToSave = async (
  obj: any,
  path = ""
): Promise<{
  newObj: any;
  largeFilesToSave: LargeFileToSave<any>[];
}> => {
  if (obj === null || isUndefined(obj)) {
    return { newObj: obj, largeFilesToSave: [] };
  }
  const largeFilesToSave: LargeFileToSave<any>[] = [];
  if (typeof obj === "string" && obj.length > LARGE_VALUE_THRESHOLD) {
    largeFilesToSave.push({
      path,
      value: obj,
      originalType: "string",
      hash: await calculateHash(obj),
    });
    return {
      newObj: {
        [OBJ_REF_VALUE]: true,
        originalType: "string",
        hash: await calculateHash(obj),
      },
      largeFilesToSave,
    };
  } else {
    const r = await detectBinaryLikeObject(obj);

    if (r) {
      const { type, hash } = r;
      largeFilesToSave.push({ path, value: obj, originalType: type, hash });
      return {
        newObj: {
          [OBJ_REF_VALUE]: true,
          originalType: type,
          hash,
        },
        largeFilesToSave,
      };
    }
  }
  let newObj: any = Array.isArray(obj) ? [] : {};
  if (typeof obj === "object") {
    for (const [key, value] of _.entries(obj)) {
      const currentPath = path ? pathJoin(path, key) : key;
      const result = await identifyLargeFilesToSave(value, currentPath);
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
      hash: obj.hash,
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
import { OBJ_REF_VALUE } from "@livestack/shared";
import { calculateHash } from "../storage/cloudStorage";

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
    largeFilesToRestore: {
      path: string;
      originalType: OriginalType;
      hash?: string;
    }[];
    fetcher: <T extends OriginalType>(
      v: LargeFileWithoutValue<T>
    ) => Promise<InferRestoredFileType<T>>;
  }) {
    const delimiter = "__zz__|||__zz__"; // Choose a custom delimiter that is unlikely to appear in path names

    const limit = pLimit(3);
    const promises = largeFilesToRestore.map((largeFile) =>
      limit(async () => {
        const value = await fetcher({
          ...largeFile,
          path: path.join(basePath, largeFile.path),
        });
        // Replace slash with the custom delimiter in path before setting the value
        const p = largeFile.path.replace(/\//g, delimiter);
        if (p === "") {
          obj_ = value;
        } else {
          _.set(obj_, p.split(delimiter), value);
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
