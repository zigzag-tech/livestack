#!/usr/bin/env node
/**
 * Make `jingway-framework` resolvable, loudly.
 *
 * livestack has no jingway submodule and this change is not the place to add
 * one. The fleet's convention is one working copy at ~/jingway that every
 * project symlinks, so this links it into node_modules and REFUSES if it is not
 * there — an unresolvable dependency that fails at the first import of a
 * production run is exactly the silent-failure shape this codebase does not
 * ship. See fleetd/README.md.
 */
import { existsSync, mkdirSync, symlinkSync, lstatSync, unlinkSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const pkg = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const source = resolve(process.env.JINGWAY_PATH ?? join(homedir(), 'jingway'));
const target = join(pkg, 'node_modules', 'jingway-framework');

if (!existsSync(join(source, 'package.json'))) {
  console.error(
    `[fleetd] jingway-framework is not at ${source}.\n` +
      `         Clone it (git@gitee.com:zigzagtech_0/jingway-framework.git) or set\n` +
      `         JINGWAY_PATH. fleetd cannot run without it; nothing was linked.`,
  );
  process.exit(1);
}
mkdirSync(dirname(target), { recursive: true });
link(source, target);
console.log(`[fleetd] jingway-framework -> ${source}`);

// zod comes from jingway's copy, ON PURPOSE. A `Handback`'s input/output are
// jingway `ZodType`s, and two zod instances in one process produce schemas that
// are structurally identical and fail `instanceof` — which surfaces as a
// validation that silently never matches rather than as a version error. One
// copy, the framework's. `kysely` likewise: the policy improver builds its own
// `Kysely` over jingway's PGLite dialect and hands it to jingway's activation
// service and migrations, which were typed and written against jingway's copy.
for (const dep of ['zod', '@types/node', 'kysely']) {
  const from = join(source, 'node_modules', dep);
  if (!existsSync(from)) {
    console.error(`[fleetd] ${source} has no node_modules/${dep}; run its install first.`);
    process.exit(1);
  }
  const to = join(pkg, 'node_modules', dep);
  mkdirSync(dirname(to), { recursive: true });
  link(from, to);
  console.log(`[fleetd] ${dep} -> ${from}`);
}

function link(from, to) {
  if (safeLstat(to)) unlinkSync(to);
  symlinkSync(from, to, 'dir');
}

function safeLstat(p) {
  try {
    return lstatSync(p);
  } catch {
    return null;
  }
}
