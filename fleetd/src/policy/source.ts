/**
 * The broker's policy record stream, as the improver reads it (task 5.1; J§9.1).
 *
 * The fleet broker records every committed target choice, and every lease outcome, with
 * the native `Recorder` (design §1, J§6.4) into
 * `$LIVESTACK_POLICY_DIR/records/livestack.fleet.choose_target.jsonl*`. The improver runs on
 * the broker host, so this is a local read of those files.
 *
 * Two things this does that `jsonlPolicyRecordSource` (jingway) does not, and why:
 *
 *  - **Oldest first.** The Recorder rotates `stem.jsonl` → `stem.jsonl.1` → … `.N`, so the
 *    OLDEST file has the HIGHEST number. A lexicographic sort reads `.1, .10, .11, .2` —
 *    newest-ish first and interleaved. Nothing downstream depends on order today
 *    (`toExposures` holds decisions until the window is read), but a reader that yields a
 *    stream should yield it in the order it was written.
 *  - **A hole in the rotation is a gap, never skipped.** Rotation renames every file up by
 *    one and deletes only the last, so `.3` missing while `.4` exists means someone removed
 *    records that were inside the stream. Reading around it would make a window that lost a
 *    day of decisions look like a quiet day. The hole is yielded as a `recorder_gap` covering
 *    the time between its neighbours (`reason: 'missing_rotation'`), which the estimators
 *    already exclude (J§7.2), and it is named in `lastRead.missing`.
 *
 * Files are opened before any is read, so a rotation that happens mid-read renames files
 * under open handles (POSIX) instead of shifting records between them.
 */
import { createReadStream } from 'node:fs';
import { open, readdir, type FileHandle } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';

import {
  parsePolicyRecordLine,
  type PolicyRecordLine,
  type PolicyRecordSource,
  type PolicyRecordWindow,
  type RecorderGapRecord,
} from 'jingway-framework/server/policy';

export const POLICY_ID = 'livestack.fleet.choose_target';

/** A gap this reader synthesised for a missing rotated file. `dropped` is unknown, not zero. */
export interface MissingRotationGap extends RecorderGapRecord {
  reason: 'missing_rotation';
  /** File names that should have been there. */
  missing: string[];
  /** Always true: how many records the missing file held is not knowable from here. */
  dropped_unknown: true;
}

export interface StreamReadStats {
  /** Files read, oldest first. */
  files: string[];
  /** Rotation slots with no file although an older one exists. Each became a gap. */
  missing: string[];
  /** Lines that were not a policy record. A broken file must not read as a quiet one. */
  unparseable: number;
  decisions: number;
  outcomes: number;
  /** `recorder_gap` lines, written by the Recorder or synthesised here. */
  gaps: number;
}

export interface StreamPolicySourceOptions {
  /** The records directory (`$LIVESTACK_POLICY_DIR/records`). */
  dir: string;
  /** The Recorder's stem. Defaults to the policy id, as `PolicyRuntime` opens it. */
  stem?: string;
}

/** `$LIVESTACK_POLICY_DIR`, defaulting exactly as `policy_runtime.default_policy_dir()`. */
export function policyDirFromEnv(env: NodeJS.ProcessEnv = process.env): string {
  return env.LIVESTACK_POLICY_DIR || path.join(homedir(), '.local/share/livestack/policy');
}

export class StreamPolicySource implements PolicyRecordSource {
  readonly dir: string;
  readonly stem: string;
  /** What the most recent `read` saw. Reset at the start of each read. */
  lastRead: StreamReadStats = emptyStats();

  constructor(options: StreamPolicySourceOptions) {
    this.dir = options.dir;
    this.stem = options.stem ?? POLICY_ID;
  }

  static fromEnv(env: NodeJS.ProcessEnv = process.env): StreamPolicySource {
    return new StreamPolicySource({ dir: path.join(policyDirFromEnv(env), 'records') });
  }

  /** The same files, as the replay CLI's `--records` glob. */
  get pattern(): string {
    return path.join(this.dir, `${this.stem}.jsonl*`);
  }

  /** Rotation slots present, oldest first, and the slots missing between them. */
  async layout(): Promise<{ files: Array<{ index: number; name: string }>; missing: number[] }> {
    const re = new RegExp(`^${this.stem.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\.jsonl(?:\\.(\\d+))?$`);
    let entries: string[];
    try {
      entries = await readdir(this.dir);
    } catch (error) {
      // No directory is no stream. Say so; an empty window must not look like a quiet one.
      throw new Error(`policy record stream ${this.dir} is not readable: ${(error as Error).message}`);
    }
    const files = entries
      .map((name) => ({ name, match: re.exec(name) }))
      .filter((e): e is { name: string; match: RegExpExecArray } => e.match !== null)
      .map(({ name, match }) => ({ index: match[1] === undefined ? 0 : Number(match[1]), name }))
      .sort((a, b) => b.index - a.index);
    const present = new Set(files.map((f) => f.index));
    const oldest = files.length > 0 ? files[0].index : -1;
    const missing: number[] = [];
    for (let i = oldest - 1; i >= 0; i--) if (!present.has(i)) missing.push(i);
    return { files, missing };
  }

  async *read(window: PolicyRecordWindow): AsyncGenerator<PolicyRecordLine> {
    const stats = emptyStats();
    this.lastRead = stats;
    const fromS = window.from.getTime() / 1000;
    const toS = window.to.getTime() / 1000;
    const { files, missing } = await this.layout();
    stats.missing = missing.map((i) => this.fileName(i));
    const missingSet = new Set(missing);

    // Open everything first: a rotation during the read then renames files under our handles.
    const handles: Array<{ index: number; name: string; handle: FileHandle }> = [];
    try {
      for (const f of files) handles.push({ ...f, handle: await open(path.join(this.dir, f.name), 'r') });
      stats.files = handles.map((h) => h.name);

      let lastTs: number | null = null;
      // Slots missing between the file just read and the next one, waiting for the next
      // record's timestamp to close their interval.
      let pending: string[] = [];
      let previousIndex = handles.length > 0 ? handles[0].index + 1 : 0;
      const gapFor = (to: number): MissingRotationGap => ({
        record: 'recorder_gap',
        reason: 'missing_rotation',
        missing: pending,
        dropped: 0,
        dropped_unknown: true,
        from_ts: lastTs ?? fromS,
        to_ts: to,
      });
      const inWindow = (gap: RecorderGapRecord) => !(gap.to_ts < fromS || gap.from_ts > toS);

      for (const file of handles) {
        for (let i = previousIndex - 1; i > file.index; i--) if (missingSet.has(i)) pending.push(this.fileName(i));
        previousIndex = file.index;
        const lines = createInterface({
          input: createReadStream('', { fd: file.handle, encoding: 'utf8', autoClose: false }),
          crlfDelay: Infinity,
        });
        for await (const line of lines) {
          if (!line.trim()) continue;
          const record = parsePolicyRecordLine(line);
          if (!record) {
            stats.unparseable++;
            continue;
          }
          const ts = record.record === 'recorder_gap' ? record.to_ts : record.ts;
          if (pending.length > 0) {
            const gap = gapFor(ts);
            pending = [];
            stats.gaps++;
            if (inWindow(gap)) yield gap;
          }
          lastTs = Math.max(lastTs ?? ts, ts);
          if (record.record === 'policy_decision') {
            if (record.ts < fromS || record.ts > toS) continue;
            stats.decisions++;
          } else if (record.record === 'policy_outcome') {
            if (record.ts < fromS) continue;
            stats.outcomes++;
          } else {
            stats.gaps++;
            if (!inWindow(record)) continue;
          }
          yield record;
        }
      }
      // The newest slot itself missing: a hole from the last record to the window's end.
      for (let i = previousIndex - 1; i >= 0; i--) if (missingSet.has(i)) pending.push(this.fileName(i));
      if (pending.length > 0) {
        const gap = gapFor(toS);
        stats.gaps++;
        if (inWindow(gap)) yield gap;
      }
    } finally {
      await Promise.all(handles.map((h) => h.handle.close()));
    }
  }

  private fileName(index: number): string {
    return index === 0 ? `${this.stem}.jsonl` : `${this.stem}.jsonl.${index}`;
  }
}

function emptyStats(): StreamReadStats {
  return { files: [], missing: [], unparseable: 0, decisions: 0, outcomes: 0, gaps: 0 };
}
