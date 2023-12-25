import fs from "fs";
import { Stream } from "stream";
import { IStorageProvider } from "./cloudStorage";
import { ensurePathExists } from "./ensurePathExists";
import { TEMP_DIR } from "./temp-dirs";
import path from "path";

export function getLocalTempFileStorageProvider(
  baseTempPath: string
): IStorageProvider {
  const pathPrefix = path.join(TEMP_DIR, baseTempPath);
  const putToStorage: IStorageProvider["putToStorage"] = async (
    destination,
    data
  ) => {
    const fullPath = `${pathPrefix}/${destination}`;
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

  return {
    putToStorage,
    uploadFromLocalPath,
    downloadFromStorage,
  };
}
