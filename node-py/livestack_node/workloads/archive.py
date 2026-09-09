"""Deterministic source bundles with no links escaping the captured tree.

Product adapters select their source closure. This generic layer captures only
explicit paths and refuses symlinks/special files rather than following them
into credentials, mutable external checkouts or runtime state.
"""
from __future__ import annotations

from contextlib import closing, ExitStack
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile

from .model import WorkloadError, encode

MANIFEST = '.harmony-source.json'


def relative_path(value):
    if not isinstance(value, str) or '\\' in value or '\x00' in value:
        raise WorkloadError('invalid source path')
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in ('..', '') for p in path.parts) or str(path) != value:
        raise WorkloadError('source path must be canonical and relative')
    return path


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def capture(root, paths, output, *, provenance=None, max_bytes=2*1024**3, max_files=50000, compression="none"):
    """Capture an explicit file set and reject a changing source during capture.

    The adapter must capture the complete dependency closure and enumerate it
    again after capture if its selection may change (new/deleted source paths).
    A source that changes during capture is retried by the caller, never mixed.
    """
    if compression not in ("none", "gzip"):
        raise WorkloadError("unsupported source compression")
    root = Path(root).resolve()
    names = sorted(set(paths))
    if not names or len(names) > max_files or MANIFEST in names:
        raise WorkloadError('invalid or oversized source file set')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='harmony-capture-', dir=output.parent) as staging:
        stage = Path(staging)/"tree"
        stage.mkdir()
        records, before, total = [], {}, 0
        for item in names:
            rel = relative_path(item)
            source = root.joinpath(*rel.parts)
            if source.resolve() != source or not source.is_file() or source.is_symlink():
                raise WorkloadError(f'source is not a private regular file: {item}')
            info = source.stat()
            if not stat.S_ISREG(info.st_mode):
                raise WorkloadError('special source file refused')
            total += info.st_size
            if total > max_bytes:
                raise WorkloadError('source byte limit exceeded', 413)
            target = stage.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            before[item] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            records.append(dict(path=item, size=info.st_size, mode=0o755 if info.st_mode & 0o111 else 0o644,
                                sha256=file_digest(target)))
        # Verify original bytes again: metadata alone is not source identity.
        for record in records:
            source = root/record['path']
            info = source.stat()
            after = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if before[record['path']] != after or file_digest(source) != record['sha256']:
                raise WorkloadError('source changed during capture', 409)
        manifest = dict(version=1, provenance=provenance or {}, files=records)
        raw = encode(manifest, 16*1024*1024).encode()
        (stage/MANIFEST).write_bytes(raw)
        temp = Path(staging)/'bundle.tar'
        modes = {r['path']: r['mode'] for r in records}
        with ExitStack() as stack:
            raw = stack.enter_context(temp.open('wb'))
            # No filename/timestamp in gzip headers: equal captured bytes yield
            # equal CAS identities across hosts and retries. Raw stays default
            # until every eligible worker understands compressed inputs.
            stream = (stack.enter_context(gzip.GzipFile(filename='', mode='wb',
                      fileobj=raw, compresslevel=3, mtime=0)) if compression == 'gzip' else raw)
            archive = stack.enter_context(tarfile.open(fileobj=stream, mode='w', format=tarfile.PAX_FORMAT))
            for item in [MANIFEST] + names:
                path = stage/item
                info = tarfile.TarInfo(item)
                info.size = path.stat().st_size
                info.mode = 0o644 if item == MANIFEST else modes[item]
                info.mtime = 0
                with path.open('rb') as stream:
                    archive.addfile(info, stream)
        if temp.stat().st_size > max_bytes:
            raise WorkloadError('source archive byte limit exceeded', 413)
        digest = file_digest(temp)
        os.replace(temp, output)
        return dict(digest=digest, size=output.stat().st_size, manifest=manifest)


def unpack(bundle, destination, expected_digest, *, max_bytes=20*1024**3, max_files=50000):
    """Verify before publishing a private tree; never call extractall()."""
    bundle, destination = Path(bundle), Path(destination)
    if file_digest(bundle) != expected_digest:
        raise WorkloadError('source bundle digest mismatch', 409)
    if destination.exists():
        raise WorkloadError('source destination already exists', 409)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.unpack-', dir=destination.parent) as tmp:
        stage, count, total, seen = Path(tmp)/'tree', 0, 0, set()
        stage.mkdir()
        with bundle.open('rb') as header:
            compressed = header.read(2) == b'\x1f\x8b'
        with tarfile.open(bundle, 'r:gz' if compressed else 'r:') as archive:
            for member in archive:
                rel = relative_path(member.name)
                if not member.isfile() or member.name in seen:
                    raise WorkloadError('duplicate, link, or special archive member refused')
                seen.add(member.name)
                count += 1
                total += member.size
                if count > max_files+1 or total > max_bytes:
                    raise WorkloadError('expanded source bound exceeded', 413)
                target = stage.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with closing(archive.extractfile(member)) as stream, target.open('xb') as out:
                    shutil.copyfileobj(stream, out, 1024*1024)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
        metadata = stage/MANIFEST
        if not metadata.is_file() or metadata.stat().st_size > 16*1024*1024:
            raise WorkloadError('source manifest absent or too large')
        try:
            manifest = json.loads(metadata.read_bytes())
            records = manifest['files']
            if manifest['version'] != 1 or not isinstance(records, list):
                raise ValueError('unknown manifest')
            expected = {r['path'] for r in records}
            if len(expected) != len(records) or expected | {MANIFEST} != seen:
                raise ValueError('manifest file set mismatch')
            for record in records:
                rel = relative_path(record['path'])
                path = stage.joinpath(*rel.parts)
                if path.stat().st_size != record['size'] or file_digest(path) != record['sha256']:
                    raise ValueError('manifest content mismatch')
                mode = stat.S_IMODE(path.stat().st_mode)
                if mode != record['mode']:
                    raise ValueError('manifest mode mismatch')
        except (ValueError, KeyError, TypeError) as exc:
            raise WorkloadError('invalid source manifest') from exc
        os.replace(stage, destination)
        return manifest
