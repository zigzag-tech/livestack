import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Readable } from "node:stream";
import test from "node:test";

import {
  casObjectKey,
  ContentAddressedStorageError,
  FilesystemContentAddressedStorage,
  parseSha256BlobId,
  sha256BlobId,
} from "./index";
import type { Sha256BlobId } from "./index";

test("filesystem CAS stores verified blobs and aliases by hash", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "livestack-cas-test-"));
  try {
    const storage = new FilesystemContentAddressedStorage({ root });
    const ref = await storage.putBlob({
      data: Readable.from(["hello ", "livestack"]),
      mediaType: "text/plain",
      alias: "render/input.txt",
    });

    assert.equal(
      ref.blobId,
      "sha256:743e75a828dbdeb02791550ed1eb32c8068694887d62752034f11f27b14ddfeb",
    );
    assert.equal(ref.size, 15);
    assert.equal(ref.mediaType, "text/plain");
    assert.equal(await storage.hasBlob(ref.blobId), true);
    assert.deepEqual(await storage.verifyBlob(ref.blobId), {
      ok: true,
      blobId: ref.blobId,
      size: 15,
    });

    assert.equal((await streamToBuffer(await storage.getBlob(ref.blobId))).toString(), "hello livestack");
    assert.deepEqual(await storage.resolveAlias("render/input.txt"), ref);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("filesystem CAS refuses corrupted objects", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "livestack-cas-test-"));
  try {
    const storage = new FilesystemContentAddressedStorage({ root });
    const ref = await storage.putBlob({ data: "original" });
    await writeFile(path.join(root, casObjectKey(ref.blobId)), "corrupted");

    const verification = await storage.verifyBlob(ref.blobId);
    assert.equal(verification.ok, false);
    assert.match(verification.error ?? "", /hash mismatch/i);
    await assert.rejects(
      () => storage.getBlob(ref.blobId),
      (error) =>
        error instanceof ContentAddressedStorageError &&
        error.code === "HASH_MISMATCH",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("filesystem CAS rejects invalid sha256 blob ids", () => {
  assert.throws(
    () => sha256BlobId("abc"),
    (error) =>
      error instanceof ContentAddressedStorageError &&
      error.code === "INVALID_BLOB_ID",
  );
  assert.throws(
    () => parseSha256BlobId("md5:0123" as Sha256BlobId),
    (error) =>
      error instanceof ContentAddressedStorageError &&
      error.code === "INVALID_BLOB_ID",
  );
});

async function streamToBuffer(stream: NodeJS.ReadableStream): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of stream) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}
