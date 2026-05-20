import { createReadStream, type ReadStream } from "node:fs";
import {
  mkdir,
  mkdtemp,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Readable } from "node:stream";

import {
  casObjectKey,
  ContentAddressedStorageError,
  hashFile,
  parseSha256BlobId,
  sha256BlobId,
  type BlobInput,
  type BlobRef,
  type BlobVerificationResult,
  type ContentAddressedStorage,
  type Sha256BlobId,
} from "@livestack/storage-cas";

export interface OssStorageClient {
  put(
    key: string,
    source: string | NodeJS.ReadableStream,
    options?: { headers?: Record<string, string> },
  ): Promise<unknown>;
  get(key: string, targetPath: string): Promise<unknown>;
  head(key: string): Promise<{
    res?: { headers?: Record<string, string | string[] | undefined> };
    meta?: Record<string, string | undefined>;
  }>;
}

export interface OssContentAddressedStorageOptions {
  bucket: string;
  client: OssStorageClient;
  tempRoot?: string;
}

export class OssContentAddressedStorage implements ContentAddressedStorage {
  constructor(private readonly options: OssContentAddressedStorageOptions) {}

  async putBlob(input: BlobInput): Promise<BlobRef> {
    const spooled = await spool(input.data, this.options.tempRoot);
    try {
      const actual = await hashFile(spooled.path);
      const blobId = sha256BlobId(actual.sha256);
      await this.options.client.put(casObjectKey(blobId), spooled.path, {
        headers: metadataHeaders({
          sha256: actual.sha256,
          size: actual.size,
          ...(input.mediaType ? { mediaType: input.mediaType } : {}),
        }),
      });
      if (input.alias) {
        await this.putAlias(input.alias, blobId);
      }
      return {
        blobId,
        sha256: actual.sha256,
        size: actual.size,
        ...(input.mediaType ? { mediaType: input.mediaType } : {}),
      };
    } finally {
      await spooled.dispose();
    }
  }

  async getBlob(blobId: Sha256BlobId): Promise<ReadStream> {
    await this.assertValidBlob(blobId);
    const tempDir = await mkdtemp(path.join(this.options.tempRoot ?? os.tmpdir(), "livestack-oss-"));
    const targetPath = path.join(tempDir, parseSha256BlobId(blobId));
    await this.options.client.get(casObjectKey(blobId), targetPath);
    const actual = await hashFile(targetPath);
    if (actual.sha256 !== parseSha256BlobId(blobId)) {
      await rm(tempDir, { recursive: true, force: true });
      throw new ContentAddressedStorageError(
        `Downloaded OSS blob hash mismatch for ${blobId}`,
        "HASH_MISMATCH"
      );
    }
    const stream = createReadStream(targetPath);
    stream.on("close", () => {
      void rm(tempDir, { recursive: true, force: true });
    });
    return stream;
  }

  async hasBlob(blobId: Sha256BlobId): Promise<boolean> {
    try {
      await this.options.client.head(casObjectKey(blobId));
      return true;
    } catch (error: any) {
      if (isNotFound(error)) return false;
      throw error;
    }
  }

  async verifyBlob(blobId: Sha256BlobId): Promise<BlobVerificationResult> {
    try {
      const metadata = await this.assertValidBlob(blobId);
      return {
        ok: true,
        blobId,
        size: metadata.size,
      };
    } catch (error: any) {
      return {
        ok: false,
        blobId,
        error: error?.message ?? String(error),
      };
    }
  }

  async putAlias(alias: string, blobId: Sha256BlobId): Promise<void> {
    const metadata = await this.assertValidBlob(blobId);
    await this.options.client.put(aliasKey(alias), Buffer.from(JSON.stringify({
      blobId,
      sha256: metadata.sha256,
      size: metadata.size,
      updatedAt: new Date().toISOString(),
    }, null, 2)), {
      headers: metadataHeaders(metadata),
    });
  }

  async resolveAlias(alias: string): Promise<BlobRef> {
    const spooled = await createTempFile(this.options.tempRoot);
    try {
      await this.options.client.get(aliasKey(alias), spooled.path);
      const parsed = JSON.parse(await readFile(spooled.path, "utf8")) as { blobId: Sha256BlobId };
      const metadata = await this.assertValidBlob(parsed.blobId);
      return {
        blobId: parsed.blobId,
        sha256: metadata.sha256,
        size: metadata.size,
        ...(metadata.mediaType ? { mediaType: metadata.mediaType } : {}),
      };
    } catch (error: any) {
      if (isNotFound(error)) {
        throw new ContentAddressedStorageError(
          `Missing OSS alias: ${alias}`,
          "MISSING_ALIAS"
        );
      }
      throw error;
    } finally {
      await spooled.dispose();
    }
  }

  private async assertValidBlob(blobId: Sha256BlobId): Promise<{
    sha256: string;
    size: number;
    mediaType?: string;
  }> {
    const expectedHash = parseSha256BlobId(blobId);
    const head = await this.options.client.head(casObjectKey(blobId));
    const metadata = parseMetadata(head);
    if (metadata.sha256 !== expectedHash) {
      throw new ContentAddressedStorageError(
        `OSS metadata hash mismatch: expected ${expectedHash}, got ${metadata.sha256}`,
        "HASH_MISMATCH"
      );
    }
    return metadata;
  }
}

export function ossUri(bucket: string, blobId: Sha256BlobId): string {
  return `oss://${bucket}/${casObjectKey(blobId)}`;
}

export function aliasKey(alias: string): string {
  return `aliases/${encodeURIComponent(alias)}.json`;
}

export function metadataHeaders(input: {
  sha256: string;
  size: number;
  mediaType?: string;
}): Record<string, string> {
  return {
    "x-oss-meta-sha256": input.sha256,
    "x-oss-meta-size": String(input.size),
    ...(input.mediaType ? { "x-oss-meta-media-type": input.mediaType } : {}),
  };
}

export function parseMetadata(head: Awaited<ReturnType<OssStorageClient["head"]>>): {
  sha256: string;
  size: number;
  mediaType?: string;
} {
  const getHeader = (name: string): string | undefined => {
    const metaKey = name.replace(/^x-oss-meta-/, "");
    const fromMeta = head.meta?.[metaKey];
    if (fromMeta) return fromMeta;
    const direct = head.res?.headers?.[name];
    if (Array.isArray(direct)) return direct[0];
    return direct;
  };
  const sha256 = getHeader("x-oss-meta-sha256");
  const rawSize = getHeader("x-oss-meta-size");
  const size = rawSize ? Number(rawSize) : NaN;
  if (!sha256 || !Number.isSafeInteger(size) || size < 0) {
    throw new ContentAddressedStorageError(
      "OSS object is missing required CAS metadata.",
      "MISSING_BLOB"
    );
  }
  return {
    sha256,
    size,
    ...(getHeader("x-oss-meta-media-type") ? { mediaType: getHeader("x-oss-meta-media-type") } : {}),
  };
}

function isNotFound(error: any): boolean {
  return error?.code === "NoSuchKey" || error?.status === 404 || error?.statusCode === 404;
}

async function spool(
  data: string | Buffer | Uint8Array | NodeJS.ReadableStream,
  tempRoot?: string
): Promise<{ path: string; dispose: () => Promise<void> }> {
  const target = await createTempFile(tempRoot);
  if (typeof data === "string" || Buffer.isBuffer(data) || data instanceof Uint8Array) {
    await writeFile(target.path, data);
  } else {
    const chunks: Buffer[] = [];
    for await (const chunk of Readable.from(data)) {
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    await writeFile(target.path, Buffer.concat(chunks));
  }
  return target;
}

async function createTempFile(tempRoot?: string): Promise<{
  path: string;
  dispose: () => Promise<void>;
}> {
  const tempDir = await mkdtemp(path.join(tempRoot ?? os.tmpdir(), "livestack-oss-"));
  await mkdir(tempDir, { recursive: true });
  return {
    path: path.join(tempDir, "blob"),
    dispose: () => rm(tempDir, { recursive: true, force: true }),
  };
}
