import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { casObjectKey, ContentAddressedStorageError } from "@livestack/storage-cas";
import {
  aliasKey,
  OssContentAddressedStorage,
  type OssStorageClient,
} from "./index";

test("OSS CAS uploads hash-addressed blobs with required metadata and aliases", async () => {
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "livestack-oss-test-"));
  try {
    const client = new MemoryOssClient();
    const storage = new OssContentAddressedStorage({
      bucket: "render-bucket",
      client,
      tempRoot,
    });

    const ref = await storage.putBlob({
      data: "hello aliyun",
      mediaType: "text/plain",
      alias: "projects/demo/input.txt",
    });

    assert.equal(
      ref.blobId,
      "sha256:fce5b5105533da8c8a60ffb6b4b47f5bf3d5604696bfdffe834579e69ac2db2a",
    );
    const object = client.objects.get(casObjectKey(ref.blobId));
    assert.ok(object);
    assert.equal(object.headers["x-oss-meta-sha256"], ref.sha256);
    assert.equal(object.headers["x-oss-meta-size"], String(ref.size));
    assert.equal(object.headers["x-oss-meta-media-type"], "text/plain");
    assert.equal(await storage.hasBlob(ref.blobId), true);
    assert.deepEqual(await storage.verifyBlob(ref.blobId), {
      ok: true,
      blobId: ref.blobId,
      size: ref.size,
    });

    const alias = client.objects.get(aliasKey("projects/demo/input.txt"));
    assert.ok(alias);
    assert.match(alias.body.toString(), new RegExp(ref.blobId));
    assert.deepEqual(await storage.resolveAlias("projects/demo/input.txt"), ref);
    assert.equal((await streamToBuffer(await storage.getBlob(ref.blobId))).toString(), "hello aliyun");
  } finally {
    await rm(tempRoot, { recursive: true, force: true });
  }
});

test("OSS CAS refuses objects without required CAS metadata", async () => {
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "livestack-oss-test-"));
  try {
    const client = new MemoryOssClient();
    const storage = new OssContentAddressedStorage({ bucket: "render-bucket", client, tempRoot });
    const ref = await storage.putBlob({ data: "metadata" });
    client.objects.get(casObjectKey(ref.blobId))!.headers = {};

    const verification = await storage.verifyBlob(ref.blobId);
    assert.equal(verification.ok, false);
    assert.match(verification.error ?? "", /missing required cas metadata/i);
    await assert.rejects(
      () => storage.getBlob(ref.blobId),
      (error) =>
        error instanceof ContentAddressedStorageError &&
        error.code === "MISSING_BLOB",
    );
  } finally {
    await rm(tempRoot, { recursive: true, force: true });
  }
});

test("OSS CAS re-hashes downloaded bytes before returning them", async () => {
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "livestack-oss-test-"));
  try {
    const client = new MemoryOssClient();
    const storage = new OssContentAddressedStorage({ bucket: "render-bucket", client, tempRoot });
    const ref = await storage.putBlob({ data: "correct" });
    client.objects.get(casObjectKey(ref.blobId))!.body = Buffer.from("wrong");

    const verification = await storage.verifyBlob(ref.blobId);
    assert.equal(verification.ok, true);
    await assert.rejects(
      () => storage.getBlob(ref.blobId),
      (error) =>
        error instanceof ContentAddressedStorageError &&
        error.code === "HASH_MISMATCH",
    );
  } finally {
    await rm(tempRoot, { recursive: true, force: true });
  }
});

class MemoryOssClient implements OssStorageClient {
  readonly objects = new Map<string, { body: Buffer; headers: Record<string, string> }>();

  async put(
    key: string,
    source: string | Buffer | NodeJS.ReadableStream,
    options?: { headers?: Record<string, string> },
  ): Promise<void> {
    this.objects.set(key, {
      body: await readSource(source),
      headers: { ...(options?.headers ?? {}) },
    });
  }

  async get(key: string, targetPath: string): Promise<void> {
    const object = this.objects.get(key);
    if (!object) throw Object.assign(new Error(`Missing ${key}`), { code: "NoSuchKey" });
    await writeFile(targetPath, object.body);
  }

  async head(key: string): Promise<{ res: { headers: Record<string, string> } }> {
    const object = this.objects.get(key);
    if (!object) throw Object.assign(new Error(`Missing ${key}`), { code: "NoSuchKey" });
    return { res: { headers: object.headers } };
  }
}

async function readSource(source: string | Buffer | NodeJS.ReadableStream): Promise<Buffer> {
  if (typeof source === "string") return readFile(source);
  if (Buffer.isBuffer(source)) return Buffer.from(source);
  return streamToBuffer(source);
}

async function streamToBuffer(stream: NodeJS.ReadableStream): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of stream) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}
