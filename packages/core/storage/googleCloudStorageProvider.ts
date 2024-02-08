import { Storage } from "@google-cloud/storage";
import fs from "fs";
import { Stream } from "stream";
import { IStorageProvider } from "./cloudStorage";
import {
  InferRestoredFileType,
  LargeFileWithoutValue,
  OriginalType,
} from "../files/file-ops";
import { Readable } from "stream";

export function getGoogleCloudStorageProvider({
  bucketName,
}: {
  bucketName: string;
}): IStorageProvider {
  const bucket = getStorageBucket(bucketName);
  const putToStorage: IStorageProvider["putToStorage"] = (path, data) => {
    return putToGoogleCloudStorage(bucketName, path, data);
  };

  // const downloadFromStorage: IStorageProvider["downloadFromStorage"] = async ({
  //   filePath,
  //   destination,
  // }) => {
  //   await bucket.file(filePath).download({ destination });
  // };

  const fetchFromStorage: IStorageProvider["fetchFromStorage"] = async <
    T extends OriginalType
  >({
    path,
    originalType,
  }: LargeFileWithoutValue<T>) => {
    const [file, resp] = await bucket.file(path).get({});

    type R = InferRestoredFileType<T>;
    if (originalType === "string") {
      return file.toString() as R;
    } else if (originalType === "buffer") {
      return data as R;
    } else if (originalType === "array-buffer") {
      return data.buffer as R;
    } else if (originalType === "blob") {
      return new Blob([data]) as R;
    } else if (originalType === "file") {
      return new File([data], path) as R;
    } else if (originalType === "stream") {
      return fs.createReadStream(path) as Readable as R;
    } else {
      throw new Error("Unsupported originalType " + originalType);
    }
  };

  // const uploadFromLocalPath: IStorageProvider["uploadFromLocalPath"] = async ({
  //   localPath,
  //   destination,
  // }) => {
  //   await bucket.upload(localPath, { destination });
  // };
  return {
    putToStorage,
    fetchFromStorage,
    // downloadFromStorage,
    // uploadFromLocalPath,
  };
}
const getStorageBucket = (bucketName: string) =>
  new Storage().bucket(bucketName);

async function putToGoogleCloudStorage(
  storageBucketName: string,
  path: string,
  data: Buffer | string | Stream | File | Blob | ArrayBuffer
): Promise<void> {
  try {
    const bucket = getStorageBucket(storageBucketName);
    const file = bucket.file(path);

    if (Buffer.isBuffer(data)) {
      // If data is a Buffer, upload directly
      await file.save(data);
    } else if (
      typeof data === "string" &&
      fs.existsSync(data) &&
      fs.lstatSync(data).isFile()
    ) {
      // If data is a file path, create a read stream and upload
      await file.save(fs.createReadStream(data));
    } else if (data instanceof Blob) {
      // If data is a Blob, convert to buffer and upload
      const buffer = await data.arrayBuffer();
      await file.save(Buffer.from(buffer));
    } else if (typeof data === "string") {
      // If data is a string (binary or base64), convert to buffer and upload
      const buffer = Buffer.from(data, isBase64(data) ? "base64" : "binary");
      await file.save(buffer);
    } else if (data instanceof ArrayBuffer) {
      // If data is an ArrayBuffer, convert to buffer and upload
      const buffer = Buffer.from(data);
      await file.save(buffer);
    } else {
      throw new Error("Invalid data type provided.");
    }

    console.log(`Data uploaded to ${path} in bucket ${storageBucketName}`);
  } catch (err) {
    console.error("Error uploading data:", err);
  }
}
/**
 * Checks if a string is Base64 encoded.
 *
 * @param {string} str - The string to check.
 * @return {boolean} True if the string is Base64 encoded, false otherwise.
 */
function isBase64(str: string): boolean {
  try {
    return Buffer.from(str, "base64").toString("base64") === str;
  } catch (e) {
    return false;
  }
}
