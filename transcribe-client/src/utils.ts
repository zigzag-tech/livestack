export function encodeToB64(int16Array: Int16Array) {
  const arr = new Uint8Array(int16Array.buffer);
  const base64String = btoa(
    arr.reduce((data, byte) => data + String.fromCharCode(byte), "")
  );
  return base64String;
}
