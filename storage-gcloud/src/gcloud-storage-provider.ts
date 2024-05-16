import { Storage } from "@google-cloud/storage";
import fs from "fs";
import { Stream } from "stream";
import {
  IStorageProvider,
  calculateHash,
  InferRestoredFileType,
  OriginalType,
} from "@livestack/core";
import { Readable } from "stream";

export function getGoogleCloudStorageProvider({
  bucketName,
  cdnPrefix,
}: {
  bucketName: string;
  cdnPrefix?: string;
}): IStorageProvider {
  const bucket = getStorageBucket(bucketName);

  return {
    putToStorage: async (destination, data) => {
      if (data instanceof Stream) {
        // stream is not hashable, upload directly
        const readable = data as Readable;
        await bucket.file(destination).save(readable);
        return;
      }
      const hash = await calculateHash(data);

      // Check if a file with the same hash already exists
      const hashRefPath = `__hash_refs/${hash}`;
      try {
        await bucket.file(hashRefPath).download();
        // File with the same hash already exists, skip uploading
        return { hash };
      } catch (error: any) {
        if (error.code !== 404) {
          console.log("Error checking hash reference", error);
          throw error;
        }
      }

      // File with the same hash doesn't exist, proceed with uploading
      const body = Buffer.isBuffer(data) ? data : Buffer.from(data.toString());
      await bucket.file(destination).save(body);

      // Store the hash reference file
      await bucket.file(hashRefPath).save(Buffer.from(destination));

      return { hash };
    },

    fetchFromStorage: async <T extends OriginalType>(f: {
      path: string;
      originalType: T;
      hash?: string;
    }) => {
      let result;
      try {
        [result] = await bucket.file(f.path).download();
      } catch (error: any) {
        if (error.code === 404 && f.hash) {
          // If the file is not found and a hash is provided, look up the hash reference
          const hashRefPath = `__hash_refs/${f.hash}`;
          try {
            const [hashRefResult] = await bucket.file(hashRefPath).download();
            const referencedPath = hashRefResult.toString();
            if (!referencedPath) {
              throw new Error("No data returned from Google Cloud Storage");
            }
            [result] = await bucket.file(referencedPath).download();
          } catch (hashRefError: any) {
            console.log(
              "Error fetching hash reference",
              `__hash_refs/${f.hash}`
            );
            throw hashRefError;
          }
        } else {
          throw error;
        }
      }
      type R = InferRestoredFileType<T>;
      if (!result)
        throw new Error("No data returned from Google Cloud Storage");

      if (f.originalType === "string") {
        return result.toString("utf-8") as R;
      } else if (f.originalType === "buffer") {
        return result as R;
      } else if (f.originalType === "array-buffer") {
        return result.buffer as R;
      } else if (f.originalType === "blob") {
        return new Blob([result]) as R;
      } else if (f.originalType === "file") {
        return new File([result], f.path) as R;
      } else if (f.originalType === "stream") {
        return Readable.from(result) as Readable as R;
      } else {
        throw new Error("Unsupported originalType " + f.originalType);
      }
    },

    getPublicUrl: (path: string) => {
      throw new Error("Not implemented");
    },
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
