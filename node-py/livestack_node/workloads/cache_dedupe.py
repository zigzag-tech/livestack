"""Hardlink byte-identical files across per-worker dependency caches (one filesystem).

    python3 -m livestack_node.workloads.cache_dedupe ROOT [ROOT ...]            # dry run: prints the saving
    python3 -m livestack_node.workloads.cache_dedupe --apply ROOT [ROOT ...]

Why it is safe: dependency-cache entries are written once and only ever read by `cp -a` (a copy, not
a link) or removed whole, and the entry digest ignores link count and mtime. A file is replaced by
linking a temporary name and renaming it over the target, so a concurrent reader sees the old or the
new inode, never a gap. Files are linked only when size, mode, uid and gid match, a full sha256 matches
AND a byte compare confirms it; files younger than --min-age seconds (default 600) are left alone.
Only regular files on the same device; symlinks, other devices and anything outside ROOTs are never
touched. Savings are reported, never inferred from a sample.
"""
import argparse
import collections
import filecmp
import hashlib
import os
from pathlib import Path
import sys
import time


def _hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 22), b''):
            digest.update(chunk)
    return digest.hexdigest()


def candidates(roots, min_size, min_age, now=None):
    now = time.time() if now is None else now
    by_size = collections.defaultdict(list)
    for root in roots:
        for current, dirs, files in os.walk(root, followlinks=False):
            for name in files:
                path = os.path.join(current, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if not os.path.isfile(path) or os.path.islink(path) or info.st_size < min_size:
                    continue
                if now-info.st_mtime < min_age:
                    continue
                by_size[(info.st_size, info.st_mode, info.st_uid, info.st_gid, info.st_dev)].append((info.st_ino, path))
    return by_size


def dedupe(roots, *, apply=False, min_size=65536, min_age=600, log=print):
    saved = linked = already = 0
    for key, entries in candidates(roots, min_size, min_age).items():
        size = key[0]
        if len(entries) < 2:
            continue
        by_hash = collections.defaultdict(list)
        for inode, path in entries:
            try:
                by_hash[_hash(path)].append((inode, path))
            except OSError:
                continue
        for group in by_hash.values():
            keeper_inode, keeper = group[0]
            for inode, path in group[1:]:
                if inode == keeper_inode:
                    already += 1
                    continue
                if not filecmp.cmp(keeper, path, shallow=False):
                    log(f'skip (hash equal but bytes differ?!): {path}')
                    continue
                saved += size
                linked += 1
                if apply:
                    temporary = f'{path}.dedupe-{os.getpid()}'
                    try:
                        os.link(keeper, temporary)
                        os.replace(temporary, path)
                    except OSError as error:
                        saved -= size
                        linked -= 1
                        log(f'skip {path}: {type(error).__name__}: {error}')
                        try:
                            os.unlink(temporary)
                        except OSError:
                            pass
    return dict(files_linked=linked, bytes_saved=saved, already_shared=already, applied=apply)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('roots', nargs='+')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--min-size', type=int, default=65536)
    parser.add_argument('--min-age', type=int, default=600)
    args = parser.parse_args(argv)
    roots = [str(Path(r).resolve()) for r in args.roots]
    result = dedupe(roots, apply=args.apply, min_size=args.min_size, min_age=args.min_age)
    print(result)
    return 0


if __name__ == '__main__':
    sys.exit(main())
