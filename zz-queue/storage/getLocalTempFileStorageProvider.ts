import fs from 'fs';
import { Stream } from 'stream';
import { IStorageProvider } from './cloudStorage';
import { ensurePathExists } from './ensurePathExists';

export function getLocalTempFileStorageProvider(basePath: string): IStorageProvider {
  const putToStorage: IStorageProvider['putToStorage'] = async (destination, data) => {
    const fullPath = `${basePath}/${destination}`;
    await ensurePathExists(fullPath);
    if (data instanceof Stream) {
      const writeStream = fs.createWriteStream(fullPath);
      data.pipe(writeStream);
    } else if (Buffer.isBuffer(data) || typeof data === 'string') {
      await fs.promises.writeFile(fullPath, data);
    } else if (data instanceof ArrayBuffer) {
      const buffer = Buffer.from(data);
      await fs.promises.writeFile(fullPath, buffer);
    } else {
      throw new Error('Unsupported data type for putToStorage');
    }
  };

  const uploadFromLocalPath: IStorageProvider['uploadFromLocalPath'] = async ({ localPath, destination }) => {
    const fullPath = `${basePath}/${destination}`;
    await ensurePathExists(fullPath);
    await fs.promises.copyFile(localPath, fullPath);
  };

  const downloadFromStorage: IStorageProvider['downloadFromStorage'] = async ({ filePath, destination }) => {
    const fullPath = `${basePath}/${filePath}`;
    await ensurePathExists(destination);
    await fs.promises.copyFile(fullPath, destination);
  };

  return {
    putToStorage,
    uploadFromLocalPath,
    downloadFromStorage,
  };
}
