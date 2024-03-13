import { Stream } from "stream";
import {
  InferRestoredFileType,
  LargeFileWithoutValue,
  OriginalType,
} from "../files/file-ops";

export interface IStorageProvider<ResourceId = string> {
  putToStorage: (
    destination: ResourceId,
    data: Buffer | string | Stream | File | Blob | ArrayBuffer
  ) => Promise<void>;
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
  projectId,
  jobId,
  key,
  storageProvider,
}: {
  projectId: string;
  jobId: string;
  key: string;
  storageProvider: IStorageProvider;
}) {
  if (!storageProvider.getPublicUrl) {
    throw new Error("storageProvider.getPublicUrl is not provided");
  }
  const fullPath = `/${projectId}/jobs/${jobId}/large-values/${key}`;
  return storageProvider.getPublicUrl(fullPath);
}
