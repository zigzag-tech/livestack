import {
  GetObjectCommandInput,
  GetObjectCommandOutput,
  PutObjectCommandInput,
  PutObjectCommandOutput,
  S3Client,
} from "@aws-sdk/client-s3";
import {
  GetObjectCommand,
  PutObjectCommand,
  HeadObjectCommand,
} from "@aws-sdk/client-s3";

import {
  IStorageProvider,
  InferRestoredFileType,
  OriginalType,
  calculateHash,
} from "@livestack/core";
import { Readable, Stream } from "stream";

export function getAWSS3StorageProvider({
  bucketName,
  // region,
  // accessKeyId,
  // accessKeySecret,
  cdnPrefix,
  region,
}: {
  bucketName: string;
  region: string;
  // region: string;
  // accessKeyId: string;
  // accessKeySecret: string;
  cdnPrefix?: string;
}): IStorageProvider {
  if (!process.env.AWS_ACCESS_KEY_ID || !process.env.AWS_SECRET_ACCESS_KEY) {
    throw new Error(
      "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set in the environment"
    );
  }
  const s3client = new S3Client({
    region,
    credentials: {
      accessKeyId: process.env.AWS_ACCESS_KEY_ID,
      secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY,
    },
  });

  return {
    putToStorage: async (destination, data) => {
      if (data instanceof Stream) {
        // stream is not hashable, upload directly
        const readable = data as Readable;
        await s3client.send<PutObjectCommandInput, PutObjectCommandOutput>(
          new PutObjectCommand({
            Bucket: bucketName,
            Key: destination,
            Body: readable,
          })
        );

        return;
      }
      const hash = await calculateHash(data);

      // Check if a file with the same hash already exists
      const hashRefPath = `__hash_refs/${hash}`;
      try {
        await s3client.send(
          new HeadObjectCommand({ Bucket: bucketName, Key: hashRefPath })
        );
        // File with the same hash already exists, skip uploading
        return { hash };
      } catch (error: any) {
        if ((error.code || error.name) !== "NotFound") {
          console.log(
            "Error checking hash reference",
            error,
            ", error code: ",
            error.code || error.name,
            ", hash: ",
            hash
          );
          throw error;
        }
      }

      // File with the same hash doesn't exist, proceed with uploading
      const body = Buffer.isBuffer(data) ? data : Buffer.from(data.toString());
      // await s3
      //   .putObject({ Bucket: bucketName, Key: destination, Body: body })
      //   .promise();
      await s3client.send(
        new PutObjectCommand({
          Bucket: bucketName,
          Key: destination,
          Body: body,
        })
      );

      // Store the hash reference file
      // await s3
      //   .putObject({ Bucket: bucketName, Key: hashRefPath, Body: destination })
      //   .promise();
      await s3client.send(
        new PutObjectCommand({
          Bucket: bucketName,
          Key: hashRefPath,
          Body: Buffer.from(destination),
        })
      );
      console.info("Uploaded to S3", destination, hashRefPath);

      return { hash };
    },

    fetchFromStorage: async <T extends OriginalType>(f: {
      path: string;
      originalType: T;
      hash?: string;
    }) => {
      let result;
      try {
        // result = await s3
        //   .getObject({ Bucket: bucketName, Key: f.path })
        //   .promise();
        result = await s3client.send(
          new GetObjectCommand({ Bucket: bucketName, Key: f.path })
        );
      } catch (error: any) {
        if ((error.code || error.name) === "NoSuchKey" && f.hash) {
          // If the file is not found and a hash is provided, look up the hash reference
          const hashRefPath = `__hash_refs/${f.hash}`;
          try {
            // const hashRefResult = await s3
            //   .getObject({ Bucket: bucketName, Key: hashRefPath })
            //   .promise();
            console.info("Fetching hash reference", hashRefPath);
            const hashRefResult = await s3client.send<
              GetObjectCommandInput,
              GetObjectCommandOutput
            >(new GetObjectCommand({ Bucket: bucketName, Key: hashRefPath }));
            const referencedPath = await hashRefResult.Body?.transformToString(
              "utf-8"
            );
            if (!referencedPath) {
              throw new Error("No data returned from S3");
            }
            // result = await s3
            //   .getObject({ Bucket: bucketName, Key: referencedPath })
            //   .promise();
            console.info("Fetching referenced path", referencedPath);
            result = await s3client.send<
              GetObjectCommandInput,
              GetObjectCommandOutput
            >(
              new GetObjectCommand({ Bucket: bucketName, Key: referencedPath })
            );
          } catch (hashRefError: any) {
            console.log(
              "Error fetching hash reference",
              `__hash_refs/${f.hash}`,
              hashRefError,
              Object.keys(hashRefError)
            );

            throw hashRefError;
          }
        } else {
          throw error;
        }
      }
      type R = InferRestoredFileType<T>;
      if (!result.Body) throw new Error("No data returned from S3");

      // Assuming result.Body is a Buffer if not streaming
      if (f.originalType === "string") {
        return (await result.Body.transformToString("utf-8")) as R; // Ensure encoding is specified if needed
      } else if (f.originalType === "buffer") {
        return Buffer.from(await result.Body.transformToByteArray()) as R;
      } else if (f.originalType === "array-buffer") {
        return (await result.Body.transformToByteArray()) as R;
      } else if (f.originalType === "blob") {
        // Here, we must ensure the Buffer type is correctly handled.
        const blob = new Blob([
          new Uint8Array(Buffer.from(await result.Body.transformToByteArray())),
        ]);
        return blob as R;
      } else if (f.originalType === "file") {
        const blob = new Blob([
          new Uint8Array(Buffer.from(await result.Body.transformToByteArray())),
        ]);
        return new File([blob], f.path) as R;
      } else if (f.originalType === "stream") {
        // Readable.from should be used directly on Buffer
        return Readable.from(
          Buffer.from(await result.Body.transformToByteArray())
        ) as Readable as R;
      } else {
        throw new Error("Unsupported originalType " + f.originalType);
      }
    },

    getPublicUrl: (path: string) => {
      throw new Error("Not implemented");
      // if (cdnPrefix) {
      //   const prefix = cdnPrefix.replace(/\/$/, ""); // Remove trailing slash
      //   const cleanedPath = path.replace(/^\//, ""); // Remove leading slash
      //   return `${prefix}/${cleanedPath}`;
      // }
      // // Otherwise, return a default S3 URL
      // return `https://${bucketName}.s3.${region}.amazonaws.com/${path}`;
    },
  };
}
