import { createHash, randomUUID } from "node:crypto";
import { createReadStream, type ReadStream } from "node:fs";
import {
  copyFile,
  mkdir,
  mkdtemp,
  readFile,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Readable } from "node:stream";

const SHA256_HEX_RE = /^[a-f0-9]{64}$/;

export type Sha256BlobId = `sha256:${string}`;

export interface BlobRef {
  blobId: Sha256BlobId;
  sha256: string;
  size: number;
  mediaType?: string;
}

export interface BlobInput {
  data: string | Buffer | Uint8Array | NodeJS.ReadableStream;
  mediaType?: string;
  alias?: string;
}

export interface BlobVerificationResult {
  ok: boolean;
  blobId: Sha256BlobId;
  size?: number;
  error?: string;
}

export interface ContentAddressedStorage {
  putBlob(input: BlobInput): Promise<BlobRef>;
  getBlob(blobId: Sha256BlobId): Promise<ReadStream>;
  hasBlob(blobId: Sha256BlobId): Promise<boolean>;
  verifyBlob(blobId: Sha256BlobId): Promise<BlobVerificationResult>;
  putAlias(alias: string, blobId: Sha256BlobId): Promise<void>;
  resolveAlias(alias: string): Promise<BlobRef>;
}

export class ContentAddressedStorageError extends Error {
  constructor(
    message: string,
    public readonly code:
      | "INVALID_BLOB_ID"
      | "HASH_MISMATCH"
      | "MISSING_BLOB"
      | "MISSING_ALIAS"
  ) {
    super(message);
    this.name = "ContentAddressedStorageError";
  }
}

export function sha256BlobId(sha256: string): Sha256BlobId {
  const normalized = sha256.toLowerCase();
  if (!SHA256_HEX_RE.test(normalized)) {
    throw new ContentAddressedStorageError(
      `Invalid sha256 hash: ${sha256}`,
      "INVALID_BLOB_ID"
    );
  }
  return `sha256:${normalized}`;
}

export function parseSha256BlobId(blobId: string): string {
  if (!blobId.startsWith("sha256:")) {
    throw new ContentAddressedStorageError(
      `Blob id must start with sha256:, got ${blobId}`,
      "INVALID_BLOB_ID"
    );
  }
  const sha256 = blobId.slice("sha256:".length).toLowerCase();
  if (!SHA256_HEX_RE.test(sha256)) {
    throw new ContentAddressedStorageError(
      `Invalid sha256 blob id: ${blobId}`,
      "INVALID_BLOB_ID"
    );
  }
  return sha256;
}

export function casObjectKey(blobId: Sha256BlobId): string {
  const sha256 = parseSha256BlobId(blobId);
  return `cas/sha256/${sha256.slice(0, 2)}/${sha256}`;
}

export function hashBytes(bytes: string | Buffer | Uint8Array): {
  sha256: string;
  size: number;
} {
  const buffer = typeof bytes === "string" ? Buffer.from(bytes) : Buffer.from(bytes);
  return {
    sha256: createHash("sha256").update(buffer).digest("hex"),
    size: buffer.byteLength,
  };
}

export async function hashFile(filePath: string): Promise<{
  sha256: string;
  size: number;
}> {
  const info = await stat(filePath);
  const hash = createHash("sha256");
  await new Promise<void>((resolve, reject) => {
    const stream = createReadStream(filePath);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", reject);
    stream.on("end", resolve);
  });
  return {
    sha256: hash.digest("hex"),
    size: info.size,
  };
}

export class FilesystemContentAddressedStorage implements ContentAddressedStorage {
  constructor(private readonly options: { root: string }) {}

  async putBlob(input: BlobInput): Promise<BlobRef> {
    const spooled = await this.spool(input.data);
    try {
      const actual = await hashFile(spooled.path);
      const blobId = sha256BlobId(actual.sha256);
      const objectPath = this.objectPath(blobId);
      await mkdir(path.dirname(objectPath), { recursive: true });
      if (!(await this.hasBlob(blobId))) {
        const tempPath = `${objectPath}.tmp-${process.pid}-${randomUUID()}`;
        await copyFile(spooled.path, tempPath);
        try {
          await rename(tempPath, objectPath);
        } catch (error: any) {
          await rm(tempPath, { force: true });
          if (error?.code !== "EEXIST") throw error;
        }
      }
      await writeFile(this.metadataPath(blobId), JSON.stringify({
        sha256: actual.sha256,
        size: actual.size,
        ...(input.mediaType ? { mediaType: input.mediaType } : {}),
      }, null, 2));
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
    return createReadStream(this.objectPath(blobId));
  }

  async hasBlob(blobId: Sha256BlobId): Promise<boolean> {
    try {
      await stat(this.objectPath(blobId));
      return true;
    } catch (error: any) {
      if (error?.code === "ENOENT") return false;
      throw error;
    }
  }

  async verifyBlob(blobId: Sha256BlobId): Promise<BlobVerificationResult> {
    try {
      const actual = await this.assertValidBlob(blobId);
      return {
        ok: true,
        blobId,
        size: actual.size,
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
    await this.assertValidBlob(blobId);
    const aliasPath = this.aliasPath(alias);
    await mkdir(path.dirname(aliasPath), { recursive: true });
    await writeFile(aliasPath, JSON.stringify({
      blobId,
      updatedAt: new Date().toISOString(),
    }, null, 2));
  }

  async resolveAlias(alias: string): Promise<BlobRef> {
    let raw: string;
    try {
      raw = await readFile(this.aliasPath(alias), "utf8");
    } catch (error: any) {
      if (error?.code === "ENOENT") {
        throw new ContentAddressedStorageError(
          `Missing CAS alias: ${alias}`,
          "MISSING_ALIAS"
        );
      }
      throw error;
    }
    const parsed = JSON.parse(raw) as { blobId: Sha256BlobId };
    const actual = await this.assertValidBlob(parsed.blobId);
    const metadata = await this.readMetadata(parsed.blobId);
    return {
      blobId: parsed.blobId,
      sha256: actual.sha256,
      size: actual.size,
      ...(metadata.mediaType ? { mediaType: metadata.mediaType } : {}),
    };
  }

  private objectPath(blobId: Sha256BlobId): string {
    return path.join(this.options.root, casObjectKey(blobId));
  }

  private metadataPath(blobId: Sha256BlobId): string {
    return `${this.objectPath(blobId)}.metadata.json`;
  }

  private aliasPath(alias: string): string {
    return path.join(this.options.root, "aliases", `${encodeURIComponent(alias)}.json`);
  }

  private async assertValidBlob(blobId: Sha256BlobId): Promise<{
    sha256: string;
    size: number;
  }> {
    const expected = parseSha256BlobId(blobId);
    const objectPath = this.objectPath(blobId);
    const actual = await hashFile(objectPath);
    if (actual.sha256 !== expected) {
      throw new ContentAddressedStorageError(
        `CAS blob hash mismatch: expected ${expected}, got ${actual.sha256}`,
        "HASH_MISMATCH"
      );
    }
    const metadata = await this.readMetadata(blobId);
    if (metadata.sha256 !== actual.sha256 || metadata.size !== actual.size) {
      throw new ContentAddressedStorageError(
        `CAS metadata mismatch for ${blobId}`,
        "HASH_MISMATCH"
      );
    }
    return actual;
  }

  private async readMetadata(blobId: Sha256BlobId): Promise<{
    sha256: string;
    size: number;
    mediaType?: string;
  }> {
    try {
      return JSON.parse(await readFile(this.metadataPath(blobId), "utf8"));
    } catch (error: any) {
      if (error?.code === "ENOENT") {
        throw new ContentAddressedStorageError(
          `Missing CAS metadata for ${blobId}`,
          "MISSING_BLOB"
        );
      }
      throw error;
    }
  }

  private async spool(
    data: string | Buffer | Uint8Array | NodeJS.ReadableStream
  ): Promise<{ path: string; dispose: () => Promise<void> }> {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "livestack-cas-"));
    const tempPath = path.join(tempDir, "blob");
    if (typeof data === "string" || Buffer.isBuffer(data) || data instanceof Uint8Array) {
      await writeFile(tempPath, data);
    } else {
      await writeStreamToFile(data, tempPath);
    }
    return {
      path: tempPath,
      dispose: () => rm(tempDir, { recursive: true, force: true }),
    };
  }
}

async function writeStreamToFile(
  stream: NodeJS.ReadableStream,
  targetPath: string
): Promise<void> {
  await mkdir(path.dirname(targetPath), { recursive: true });
  const chunks: Buffer[] = [];
  for await (const chunk of Readable.from(stream)) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  await writeFile(targetPath, Buffer.concat(chunks));
}
