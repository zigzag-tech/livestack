import { Storage } from "@google-cloud/storage";
import fs from "fs";
import { Stream } from "stream";

export interface IStorageProvider {
  putToStorage: (
    path: string,
    data: Buffer | string | Stream | File | Blob | ArrayBuffer
  ) => Promise<void>;
  uploadFromLocalPath: (p: {
    localPath: string;
    destination: string;
  }) => Promise<void>;
  downloadFromStorage: (p: {
    filePath: string;
    destination: string;
  }) => Promise<void>;
}

export function getGoogleCloudStorageProvider({
  bucketName,
}: {
  bucketName: string;
}): IStorageProvider {
  const bucket = getStorageBucket(bucketName);
  const putToStorage: IStorageProvider["putToStorage"] = (path, data) => {
    return putToGoogleCloudStorage(bucketName, path, data);
  };

  const downloadFromStorage: IStorageProvider["downloadFromStorage"] = async ({
    filePath,
    destination,
  }) => {
    await bucket.file(filePath).download({ destination });
  };

  const uploadFromLocalPath: IStorageProvider["uploadFromLocalPath"] = async ({
    localPath,
    destination,
  }) => {
    await bucket.upload(localPath, { destination });
  };
  return {
    putToStorage,
    downloadFromStorage,
    uploadFromLocalPath,
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
