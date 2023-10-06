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
