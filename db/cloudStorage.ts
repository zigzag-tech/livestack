import { Storage } from "@google-cloud/storage";

export const getStorageBucket = (bucketName: string) =>
  new Storage().bucket(bucketName);
