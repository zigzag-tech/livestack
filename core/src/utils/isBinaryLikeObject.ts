import { Stream } from "stream";
export const detectBinaryLikeObject = (
  obj: any
): false | "array-buffer" | "stream" | "blob" | "file" | "buffer" => {
  if (obj instanceof ArrayBuffer) {
    return "array-buffer";
  }
  if (obj instanceof Stream) {
    return "stream";
  }
  if (typeof Blob !== "undefined" && obj instanceof Blob) {
    return "blob";
  }
  if (typeof File !== "undefined" && obj instanceof File) {
    return "file";
  }
  if (typeof Buffer !== "undefined" && Buffer.isBuffer(obj)) {
    return "buffer";
  }
  return false;
};
