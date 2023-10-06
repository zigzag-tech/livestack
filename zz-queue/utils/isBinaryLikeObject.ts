export const isBinaryLikeObject = (obj: any): boolean => {
  if (obj instanceof ArrayBuffer) {
    return true;
  }
  if (typeof Blob !== "undefined" && obj instanceof Blob) {
    return true;
  }
  if (typeof File !== "undefined" && obj instanceof File) {
    return true;
  }
  if (typeof Buffer !== "undefined" && Buffer.isBuffer(obj)) {
    return true;
  }
  return false;
};
