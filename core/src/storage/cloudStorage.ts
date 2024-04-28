import { Stream } from "stream";
import {
  InferRestoredFileType,
  LargeFileWithoutValue,
  OriginalType,
} from "../files/file-ops";
import crypto from "crypto";

export interface IStorageProvider<ResourceId = string> {
  putToStorage: (
    destination: ResourceId,
    data: Buffer | string | Stream | File | Blob | ArrayBuffer
  ) => Promise<{ hash: string } | void>;
  // uploadFromLocalPath: (p: {
  //   localPath: string;
  //   destination: string;
  // }) => Promise<void>;
  // downloadFromStorage: (p: {
  //   filePath: string;
  //   destination: string;
  // }) => Promise<void>;
  fetchFromStorage: <T extends OriginalType>(f: {
    path: ResourceId;
    originalType: T;
    hash?: string;
  }) => Promise<InferRestoredFileType<T>>;
  getPublicUrl?: (path: ResourceId) => string;
}

export const saveLargeFilesToStorage = async (
  largeFilesToSave: { path: string; value: any }[],
  storageProvider: IStorageProvider
): Promise<void> => {
  for (const { path, value } of largeFilesToSave) {
    await storageProvider.putToStorage(path, value);
  }
};

export function getPublicCdnUrl({
  projectUuid,
  jobId,
  key,
  storageProvider,
}: {
  projectUuid: string;
  jobId: string;
  key: string;
  storageProvider: IStorageProvider;
}) {
  if (!storageProvider.getPublicUrl) {
    throw new Error("storageProvider.getPublicUrl is not provided");
  }
  const fullPath = `/${projectUuid}/jobs/${jobId}/large-values/${key}`;
  return storageProvider.getPublicUrl(fullPath);
}

export async function calculateHash(
  data: Buffer | string | ArrayBuffer | Blob | File
): Promise<string> {
  const hasher = crypto.createHash("sha256");
  if (data instanceof Buffer) {
    hasher.update(data);
  } else if (data instanceof ArrayBuffer) {
    hasher.update(Buffer.from(data));
  } else if (data instanceof File || data instanceof Blob) {
    const arrayBuffer = await new Promise<ArrayBuffer>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.readAsArrayBuffer(data);
    });
    hasher.update(Buffer.from(arrayBuffer));
  } else {
    hasher.update(data.toString());
  }
  return hasher.digest("hex");
}
