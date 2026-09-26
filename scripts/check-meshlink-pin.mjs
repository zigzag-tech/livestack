#!/usr/bin/env node
// check-meshlink-pin.mjs — the MESHLINK.lock pin must match the sibling checkout.
//
// WHY THIS EXISTS (DR-5). Livestack consumes meshlink as a SIBLING CHECKOUT
// (pubspec/cargo path deps), and a path dependency carries no version: "which
// meshlink" is whatever happens to be checked out next to you. Two machines
// can build — and mint tokens against — different software from one livestack
// commit, and nothing says so. MESHLINK.lock records the intended commit and
// this check compares it to the sibling's HEAD, naming both hashes on a
// mismatch. Adapted from benchday's scripts/check-submodule-pins.mjs (the
// meshlink half), which owns the same discipline for its own pin.
//
// The pin covers BOTH the pyo3 mesh-route crate and the packages/mesh_relay
// formats — they ship together from one meshlink commit, and a recorded pin
// that nothing compares is not a pin; it reads like one, which is worse than
// having none because it answers "which meshlink" with a number nobody checks.
//
// Every not-verifiable state is a FAILURE, never a pass: a missing lock, an
// unparseable pin line, or an unreadable sibling all exit 1. An unset pin
// must not read as "no pin required" (repo rule 13 — absence and failure must
// not look alike).
//
//   node scripts/check-meshlink-pin.mjs              # check (CI entry point)
//   node scripts/check-meshlink-pin.mjs --fix        # re-pin to the sibling's HEAD
//
// Overrides for tests and nonstandard layouts:
//   --lock <path>      lock file (default: <repo>/MESHLINK.lock)
//   --sibling <dir>    meshlink checkout (default: <repo>/../meshlink)
//
// Exit 0 = lock present, pin parses, sibling HEAD agrees.
// Exit 1 = anything else, with the reason named.

import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DEFAULT_LOCK = resolve(root, 'MESHLINK.lock');
const DEFAULT_SIBLING = resolve(root, '..', 'meshlink');

function argValue(flag, fallback) {
  const i = process.argv.indexOf(flag);
  return i >= 0 && process.argv[i + 1] ? resolve(process.argv[i + 1]) : fallback;
}

const LOCK_PATH = argValue('--lock', DEFAULT_LOCK);
const SIBLING = argValue('--sibling', DEFAULT_SIBLING);

export function readPin(lockText) {
  const m = lockText.match(/^MESHLINK_REV=([0-9a-f]{7,40})\s*$/m);
  return m ? m[1] : null;
}

/// Compare the recorded pin to the sibling's HEAD.
///
/// Returns a problem string, or null when they agree. Pure, so the fail-closed
/// cases are unit-testable without a fleet. `dirExists`/`headRev` are injected
/// because every state that cannot be verified must surface as a problem here,
/// not as an exception the caller might swallow.
export function meshlinkPinProblem(lockText, headRev, { lockPath = 'MESHLINK.lock', sibling = '../meshlink', dirExists = true } = {}) {
  if (lockText == null) return `${lockPath}: missing — the meshlink pin is not recorded`;
  const pinned = readPin(lockText);
  if (!pinned) return `${lockPath}: no MESHLINK_REV=<sha> line`;
  if (!dirExists) {
    return `${lockPath} pins ${pinned} but ${sibling} is not there — clone meshlink beside this checkout`;
  }
  if (!headRev) return `${lockPath} pins ${pinned} but ${sibling} HEAD could not be read`;
  if (!headRev.startsWith(pinned) && !pinned.startsWith(headRev)) {
    return (
      `meshlink pin mismatch:\n` +
      `      ${lockPath} pins  ${pinned}\n` +
      `      ${sibling} HEAD is  ${headRev}\n` +
      `    Take the sibling's commit deliberately: re-run with --fix and commit\n` +
      `    the lock alongside the change that needs it, or check the sibling out at the pin.`
    );
  }
  return null;
}

function main() {
  const lockText = existsSync(LOCK_PATH) ? readFileSync(LOCK_PATH, 'utf8') : null;
  const dirExists = existsSync(SIBLING);
  let headRev = null;
  if (dirExists) {
    try {
      headRev = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: SIBLING, encoding: 'utf8' }).trim();
    } catch { /* reported as unreadable below */ }
  }

  if (process.argv.includes('--fix')) {
    if (!headRev) {
      console.error(`check-meshlink-pin: --fix cannot read ${SIBLING} HEAD`);
      process.exit(1);
    }
    const body = (lockText ?? '').replace(/^MESHLINK_REV=.*$/m, `MESHLINK_REV=${headRev}`);
    writeFileSync(LOCK_PATH, body.includes(headRev) ? body : `${body.trimEnd()}\nMESHLINK_REV=${headRev}\n`);
    console.log(`check-meshlink-pin: re-pinned meshlink to ${headRev}`);
    return;
  }

  const problem = meshlinkPinProblem(lockText, headRev, {
    lockPath: LOCK_PATH, sibling: SIBLING, dirExists,
  });
  if (problem) {
    console.error('check-meshlink-pin: FAILED\n');
    console.error(`  ${problem}\n`);
    process.exit(1);
  }
  console.log(`check-meshlink-pin: ok (pin ${readPin(lockText)} matches ${SIBLING} HEAD)`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
