"""General builder for harmony-handler-package.v1 releases.

Builds immutable handler packages from a directory of files: a canonical
manifest (validated by ``handler_release.validate_manifest`` so the digest is
the registry's own), a deterministic ustar archive, and the bundle layout that
``handler_release_cli stage --bundle`` reads::

    <out>/manifest.json                       {"handler_releases": {<handler>: descriptor}}
    <out>/handler-packages/<slug>-<digest>.tar
    <out>/handler-packages/<slug>-<digest>.json   {handler_id, release_digest, archive_digest, archive_path, manifest}

The builder is application-neutral. The runtime is named by id and must be in
the operator-installed set the caller passes; a package never carries an
interpreter path, policy, credential or endpoint. Links, special files and
paths that are not plain relative files are refused rather than followed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

from .handler_release import MAX_PACKAGE_BYTES, MAX_PACKAGE_FILES, validate_manifest
from .model import WorkloadError

DEFAULT_RUNTIME_IDS = ('node22', 'python3')
_SKIP_TOP = {'manifest.json', 'worker-handlers.json', 'handler-packages'}


def _fail(reason):
    raise WorkloadError(reason, 400)


def _utf8_key(value):
    return value.encode('utf-8')


def _walk(root, include_paths):
    root = Path(root)
    found = {}

    def visit(directory):
        for entry in sorted(os.scandir(directory), key=lambda item: _utf8_key(item.name)):
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if relative in _SKIP_TOP:
                continue
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                _fail(f'handler_package_link_refused:{relative}')
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                if include_paths is None or relative in include_paths:
                    found[relative] = (path, info)
            else:
                _fail(f'handler_package_special_file_refused:{relative}')

    visit(root)
    if include_paths is not None:
        missing = sorted(set(include_paths) - set(found), key=_utf8_key)
        if missing:
            _fail('handler_package_closure_missing:' + ','.join(missing))
    return [(relative, *found[relative]) for relative in sorted(found, key=_utf8_key)]


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _octal(value, width, field):
    text = f'{value:o}'.rjust(width - 1, '0') + '\0'
    if len(text) != width:
        _fail(f'handler_tar_{field}_overflow')
    return text.encode('ascii')


def _header(path, mode, size):
    raw = path.encode('utf-8')
    name, prefix = raw, b''
    if len(raw) > 100:
        split = -1
        index = raw.rfind(b'/')
        while index > 0:
            if len(raw) - index - 1 <= 100 and index <= 155:
                split = index
                break
            index = raw.rfind(b'/', 0, index)
        if split < 0:
            _fail('handler_tar_path_too_long')
        prefix, name = raw[:split], raw[split + 1:]
    header = bytearray(512)
    header[0:len(name)] = name
    header[100:108] = _octal(mode, 8, 'mode')
    header[108:116] = _octal(0, 8, 'uid')
    header[116:124] = _octal(0, 8, 'gid')
    header[124:136] = _octal(size, 12, 'size')
    header[136:148] = _octal(0, 12, 'mtime')
    header[148:156] = b' ' * 8
    header[156] = 0x30
    header[257:263] = b'ustar\0'
    header[263:265] = b'00'
    header[345:345 + len(prefix)] = prefix
    header[148:156] = f'{sum(header):06o}'.encode('ascii') + b'\0 '
    return bytes(header)


def _write_tar(files, destination):
    temporary = Path(f'{destination}.tmp-{os.getpid()}')
    try:
        with open(temporary, 'xb') as out:
            for item in files:
                out.write(_header(item['path'], item['mode'], item['size']))
                digest, size = hashlib.sha256(), 0
                with open(item['absolute'], 'rb') as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b''):
                        size += len(chunk)
                        digest.update(chunk)
                        out.write(chunk)
                if size != item['size'] or digest.hexdigest() != item['sha256']:
                    _fail(f'handler_package_changed_while_archiving:{item["path"]}')
                out.write(b'\0' * ((512 - size % 512) % 512))
            out.write(b'\0' * 1024)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_release_package(root, out_dir, *, handler_id, release_version, runtime_id, entrypoint,
                          payload_schema, result_schema, platform, architecture,
                          arguments=(), outputs=(), infrastructure_outputs=(),
                          infrastructure_exit_codes=(), backend='native', include_paths=None,
                          allowed_runtime_ids=DEFAULT_RUNTIME_IDS):
    """Build one immutable release and return its descriptor.

    ``root`` is the package content; ``out_dir`` receives ``handler-packages/``
    and must not lie inside ``root``.
    """
    root, out_dir = Path(root).resolve(), Path(out_dir).resolve()
    if out_dir == root or root in out_dir.parents:
        _fail('handler_package_output_inside_root')
    if runtime_id not in allowed_runtime_ids:
        _fail('handler_runtime_not_operator_installed')
    selected = _walk(root, None if include_paths is None else set(include_paths))
    if not 1 <= len(selected) <= MAX_PACKAGE_FILES:
        _fail('handler_manifest_invalid_file_inventory')
    files, total = [], 0
    for relative, absolute, info in selected:
        total += info.st_size
        if total > MAX_PACKAGE_BYTES:
            _fail('handler_package_bytes_exceeded')
        files.append(dict(path=relative, mode=0o555 if info.st_mode & 0o111 else 0o444,
                          size=info.st_size, sha256=_sha256(absolute), absolute=absolute))
    manifest = dict(
        format='harmony-handler-package.v1', handler_id=handler_id, release_version=release_version,
        execution_contract=1, payload_schema=payload_schema, result_schema=result_schema,
        platform=platform, architecture=architecture, backend=backend, runtime_id=runtime_id,
        entrypoint=entrypoint, arguments=list(arguments),
        outputs=sorted(set(outputs), key=_utf8_key),
        infrastructure_outputs=sorted(set(infrastructure_outputs), key=_utf8_key),
        infrastructure_exit_codes=sorted(set(infrastructure_exit_codes)),
        files=[{key: item[key] for key in ('path', 'mode', 'size', 'sha256')} for item in files])
    checked = validate_manifest(manifest)
    directory = out_dir/'handler-packages'
    directory.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '-', handler_id)
    archive = directory/f'{slug}-{checked["release_digest"]}.tar'
    _write_tar(files, archive)
    descriptor = dict(handler_id=handler_id, release_digest=checked['release_digest'],
                      archive_digest=_sha256(archive),
                      archive_path=archive.relative_to(out_dir).as_posix(), manifest=manifest)
    archive.with_suffix('.json').write_text(json.dumps(descriptor, indent=2) + '\n')
    return descriptor


def build_bundle(root, out_dir, *, release_version, platform, architecture, handlers,
                 include_paths=None, allowed_runtime_ids=DEFAULT_RUNTIME_IDS):
    """Build one package per handler from the same root and write the stage bundle manifest.

    ``handlers`` maps handler id to the per-handler fields of
    ``build_release_package`` (runtime_id, entrypoint, arguments, outputs,
    schemas, exit codes, optional backend).
    """
    if not handlers:
        _fail('handler_bundle_empty')
    releases = {}
    for handler_id in sorted(handlers):
        descriptor = build_release_package(
            root, out_dir, handler_id=handler_id, release_version=release_version, platform=platform,
            architecture=architecture, include_paths=include_paths,
            allowed_runtime_ids=allowed_runtime_ids, **handlers[handler_id])
        releases[handler_id] = {key: descriptor[key] for key in ('release_digest', 'archive_digest', 'archive_path')}
    manifest = Path(out_dir)/'manifest.json'
    manifest.write_text(json.dumps({'handler_releases': releases}, indent=2, sort_keys=True) + '\n')
    return releases


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, help='directory holding the package files')
    parser.add_argument('--out', required=True, help='bundle output directory (outside --root)')
    parser.add_argument('--spec', required=True, help='JSON: release_version, platform, architecture, handlers{}')
    parser.add_argument('--runtime-id', action='append', help='operator-installed runtime id (repeatable)')
    args = parser.parse_args(argv)
    try:
        spec = json.loads(Path(args.spec).read_text())
        releases = build_bundle(args.root, args.out, release_version=spec['release_version'],
            platform=spec['platform'], architecture=spec['architecture'], handlers=spec['handlers'],
            include_paths=spec.get('include_paths'),
            allowed_runtime_ids=tuple(args.runtime_id or DEFAULT_RUNTIME_IDS))
    except (OSError, ValueError, KeyError, TypeError, WorkloadError) as error:
        print(f'handler_package_build_refused: {error}', file=sys.stderr)
        return 2
    print(json.dumps(releases, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
