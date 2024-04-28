import { Stream } from "stream";
import { calculateHash } from "../storage/cloudStorage";
export const detectBinaryLikeObject = async (
  obj: any
): Promise<
  | {
      type: "array-buffer" | "stream" | "blob" | "file" | "buffer";
      hash?: string;
    }
  | false
> => {
  if (obj instanceof ArrayBuffer) {
    return {
      type: "array-buffer",
      hash: await calculateHash(obj),
    };
  }
  if (obj instanceof Stream) {
    return {
      type: "stream",
    };
  }
  if (typeof Blob !== "undefined" && obj instanceof Blob) {
    return {
      type: "blob",
      hash: await calculateHash(obj),
    };
  }
  if (typeof File !== "undefined" && obj instanceof File) {
    return {
      type: "file",
      hash: await calculateHash(obj),
    };
  }
  if (typeof Buffer !== "undefined" && Buffer.isBuffer(obj)) {
    return {
      type: "buffer",
      hash: await calculateHash(obj),
    };
  }
  return false;
};
