import fs from "fs";
import { Stream } from "stream";
import { IStorageProvider } from "./cloudStorage";
import { ensurePathExists } from "./ensurePathExists";
import { TEMP_DIR } from "./temp-dirs";
import path from "path";
import {
  InferRestoredFileType,
  LargeFileWithoutValue,
  OriginalType,
} from "../files/file-ops";
import { Readable } from "stream";

export function getLocalTempFileStorageProvider(
  baseTempPath: string
): IStorageProvider {
  const pathPrefix = path.join(TEMP_DIR, baseTempPath);
  const putToStorage: IStorageProvider["putToStorage"] = async (
    destination,
    data
  ) => {
    const fullPath = `${pathPrefix}/${destination}`;
    // console.debug("Saving to", fullPath, data instanceof Stream);
    // console.debug("saving to", fullPath);
    await ensurePathExists(path.dirname(fullPath));
    if (data instanceof Stream) {
      const writeStream = fs.createWriteStream(fullPath);
      data.pipe(writeStream);
    } else if (Buffer.isBuffer(data) || typeof data === "string") {
      await fs.promises.writeFile(fullPath, data);
    } else if (data instanceof ArrayBuffer) {
      const buffer = Buffer.from(data);
      await fs.promises.writeFile(fullPath, buffer);
    } else {
      throw new Error("Unsupported data type for putToStorage");
    }
  };

  const uploadFromLocalPath: IStorageProvider["uploadFromLocalPath"] = async ({
    localPath,
    destination,
  }) => {
    const fullPath = `${pathPrefix}/${destination}`;
    await ensurePathExists(path.dirname(fullPath));
    await fs.promises.copyFile(localPath, fullPath);
  };

  const downloadFromStorage: IStorageProvider["downloadFromStorage"] = async ({
    filePath,
    destination,
  }) => {
    const fullPath = `${pathPrefix}/${filePath}`;
    await ensurePathExists(path.dirname(fullPath));
    await fs.promises.copyFile(fullPath, destination);
  };

  const fetchFromStorage = async <T extends OriginalType>(
    f: LargeFileWithoutValue<T>
  ) => {
    const fullPath = `${pathPrefix}/${f.path}`;
    const data = await fs.promises.readFile(fullPath);
    type R = InferRestoredFileType<T>;
    if (f.originalType === "string") {
      return data.toString() as R;
    } else if (f.originalType === "buffer") {
      return data as R;
    } else if (f.originalType === "array-buffer") {
      return data.buffer as R;
    } else if (f.originalType === "blob") {
      return new Blob([data]) as R;
    } else if (f.originalType === "file") {
      return new File([data], fullPath) as R;
    } else if (f.originalType === "stream") {
      return fs.createReadStream(fullPath) as Readable as R;
    } else {
      throw new Error("Unsupported originalType " + f.originalType);
    }
  };

  return {
    putToStorage,
    uploadFromLocalPath,
    downloadFromStorage,
    fetchFromStorage,
  };
}
