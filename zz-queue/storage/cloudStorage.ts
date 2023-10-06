import { Stream } from "stream";

export interface IStorageProvider {
  putToStorage: (
    destination: string,
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
  getPublicUrl?: (path: string) => string;
}
