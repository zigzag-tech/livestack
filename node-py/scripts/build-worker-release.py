#!/usr/bin/env python3
"""Build, hash and verify a Harmony workload-worker release from a git commit.

A release is the directory a worker unit puts on PYTHONPATH:

    <release>/node-py/LICENSE
    <release>/node-py/livestack_node/**      (no tests, no __pycache__)
    <release>/node-py/RELEASE.json           (commit + content hash; not hashed)

Subcommands (see node-py/docs/worker-release-rollout.md):

    build  COMMIT --out DIR        materialise COMMIT's tree into DIR (refuses a non-empty DIR)
    hash   DIR                     print the content hash of a release directory
    verify BUILT DEPLOYED [--ssh HOST] [--commit REF]
                                   exit 0 if identical; otherwise list files only in one side
                                   or changed. DEPLOYED may be on HOST (streamed over ssh, nothing
                                   is written there). With --commit, each changed file is
                                   attributed to the newest commit on REF whose version of it is
                                   the deployed one, or flagged HAND-EDIT when no commit has it.
    verifier-drift RELEASE SDK [--ssh HOST]
                                   exit 0 if every launch-contract module (VERIFIER_CONTRACT)
                                   is byte-identical in RELEASE (node-py/livestack_node)
                                   and the installed verifier SDK (/opt/livestack-compilation/
                                   verifier-sdk-<hash>/livestack_node). Run it in the same step as
                                   a release roll; exit 1 names the files, and the fix is a new
                                   SDK plus the drop-in repoint (worker-release-rollout.md).

The content hash is sha256 over the sorted lines "<relpath>\\0<sha256(file)>\\n" of
every regular file under node-py/ except RELEASE.json, __pycache__ and *.pyc. File modes
and mtimes are deliberately not part of it.
"""
import argparse, hashlib, io, json, os, subprocess, sys, tarfile
from pathlib import Path

PATHS = ('node-py/livestack_node', 'LICENSE')   # repo-root LICENSE ships as node-py/LICENSE
IGNORED_NAMES = {'RELEASE.json'}


def ignored(rel):
    parts = rel.split('/')
    return '__pycache__' in parts or rel.endswith('.pyc') or parts[-1] in IGNORED_NAMES


def tree_digests(root):
    """{relpath under node-py/: sha256} of a release directory."""
    base = Path(root)/'node-py'
    if not base.is_dir():
        sys.exit(f'{root} has no node-py/ directory')
    out = {}
    for path in sorted(base.rglob('*')):
        rel = path.relative_to(base).as_posix()
        if path.is_file() and not path.is_symlink() and not ignored(rel):
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def digests_from_tar(stream):
    out = {}
    with tarfile.open(fileobj=stream, mode='r|') as tar:
        for member in tar:
            rel = member.name.removeprefix('./').removeprefix('node-py/')
            if member.isfile() and not ignored(rel) and member.name.removeprefix('./').startswith('node-py/'):
                out[rel] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
    return out


def content_hash(digests):
    h = hashlib.sha256()
    for rel in sorted(digests):
        h.update(f'{rel}\0{digests[rel]}\n'.encode())
    return h.hexdigest()


REPO = Path(__file__).resolve().parents[2]   # always run git at the repo root, whatever the cwd


def git(*args):
    return subprocess.run(('git', '-C', str(REPO), *args), check=True, capture_output=True).stdout


def build(commit, out):
    sha = git('rev-parse', '--verify', commit+'^{commit}').decode().strip()
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        sys.exit(f'{out} is not empty; refusing to overlay')
    out.mkdir(parents=True, exist_ok=True)
    archive = git('archive', sha, '--', *PATHS)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(out, filter='data')
    (out/'LICENSE').rename(out/'node-py'/'LICENSE')
    for pyc in out.rglob('__pycache__'):
        raise SystemExit(f'unexpected {pyc}')
    digests = tree_digests(out)
    meta = dict(commit=sha, content_hash=content_hash(digests), files=len(digests))
    (out/'node-py'/'RELEASE.json').write_text(json.dumps(meta, indent=1)+'\n')
    print(f"{meta['content_hash']}  {meta['files']} files  commit {sha}  -> {out}")


def deployed_digests(path, host):
    if host is None:
        return tree_digests(path)
    proc = subprocess.Popen(['ssh', host, f"cd {path} && tar cf - --exclude='__pycache__' --exclude='*.pyc' node-py"],
                            stdout=subprocess.PIPE)
    digests = digests_from_tar(proc.stdout)
    if proc.wait():
        sys.exit(f'ssh {host} tar of {path} failed')
    return digests


def attribute(rel, deployed_sha, ref):
    """Newest commit on REF whose node-py/<rel> hashes to the deployed sha256, else None."""
    full = 'node-py/'+rel
    for commit in git('log', '--format=%H', ref, '--', full).decode().split():
        try:
            blob = git('show', f'{commit}:{full}')
        except subprocess.CalledProcessError:
            continue                      # deleted in that commit
        if hashlib.sha256(blob).hexdigest() == deployed_sha:
            return commit
    return None


def verify(built, deployed, host, ref):
    a, b = tree_digests(built), deployed_digests(deployed, host)
    only_built, only_dep = sorted(set(a)-set(b)), sorted(set(b)-set(a))
    changed = sorted(r for r in set(a) & set(b) if a[r] != b[r])
    print(f'built    {content_hash(a)}  ({len(a)} files)')
    print(f'deployed {content_hash(b)}  ({len(b)} files)')
    if not (only_built or only_dep or changed):
        print('IDENTICAL')
        return 0
    for rel in only_built: print(f'only in built     {rel}')
    for rel in only_dep:   print(f'only in deployed  {rel}' + (_note(rel, b[rel], ref) if ref else ''))
    for rel in changed:    print(f'changed           {rel}' + (_note(rel, b[rel], ref) if ref else ''))
    print(f'DIFFERENT: {len(only_built)} only-built, {len(only_dep)} only-deployed, {len(changed)} changed')
    return 1


def _note(rel, deployed_sha, ref):
    commit = attribute(rel, deployed_sha, ref)
    return f'   [deployed copy = {commit[:8]} on {ref}]' if commit else '   [HAND-EDIT: no commit on ' + ref + ' has the deployed copy]'


# The modules that define the launch contract between the in-attempt guard and the root verifier
# (request/receipt shape, class set, registry trust rules, authority hop). The verifier also imports
# general modules (model, transport); those are not compared, because they change in unrelated
# releases and a difference there is not a contract change.
VERIFIER_CONTRACT = tuple(f'workloads/{m}.py' for m in
    ('launch_contract', 'launch_verifier', 'launch_guard', 'compilation_policy', 'windows_proc', 'darwin_proc'))


def verifier_modules(base):
    return [rel for rel in VERIFIER_CONTRACT if (Path(base)/rel).is_file()]


def verifier_drift(release, sdk, host):
    """0 when the verifier SDK carries the release's version of every module it loads."""
    rel_base = Path(release)/'node-py'/'livestack_node'
    modules = verifier_modules(rel_base)
    sdk_digests = deployed_digests_flat(sdk, host)
    drift = []
    for rel in modules:
        want = hashlib.sha256((rel_base/rel).read_bytes()).hexdigest()
        have = sdk_digests.get(rel)
        if have != want:
            drift.append(f'{"missing in SDK" if have is None else "differs"}  {rel}')
    print(f'launch-contract modules compared: {len(modules)} from {release}')
    if not drift:
        print('VERIFIER SDK CURRENT')
        return 0
    print('\n'.join(drift))
    print(f'VERIFIER SDK DRIFT: {len(drift)} of {len(modules)} modules; stage a verifier SDK built from this release '
          f'and repoint the harmony-compilation-* drop-ins before rolling workers')
    return 1


def deployed_digests_flat(sdk, host):
    """{relpath under <sdk>/livestack_node: sha256}, locally or over ssh (nothing written there)."""
    if host is None:
        base = Path(sdk)/'livestack_node'
        if not base.is_dir():
            sys.exit(f'{sdk} has no livestack_node/ directory')
        return {p.relative_to(base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in base.rglob('*.py') if p.is_file()}
    proc = subprocess.Popen(['ssh', host, f"cd {sdk} && tar cf - --exclude='__pycache__' livestack_node"], stdout=subprocess.PIPE)
    out = {}
    with tarfile.open(fileobj=proc.stdout, mode='r|') as tar:
        for member in tar:
            name = member.name.removeprefix('./')
            if member.isfile() and name.startswith('livestack_node/') and name.endswith('.py'):
                out[name.removeprefix('livestack_node/')] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
    if proc.wait():
        sys.exit(f'ssh {host} tar of {sdk} failed')
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build'); b.add_argument('commit'); b.add_argument('--out', required=True)
    h = sub.add_parser('hash'); h.add_argument('dir')
    v = sub.add_parser('verify'); v.add_argument('built'); v.add_argument('deployed')
    v.add_argument('--ssh'); v.add_argument('--commit')
    d = sub.add_parser('verifier-drift'); d.add_argument('release'); d.add_argument('sdk'); d.add_argument('--ssh')
    args = p.parse_args()
    if args.cmd == 'verifier-drift': sys.exit(verifier_drift(args.release, args.sdk, args.ssh))
    if args.cmd == 'build': build(args.commit, args.out)
    elif args.cmd == 'hash': print(content_hash(tree_digests(args.dir)))
    else: sys.exit(verify(args.built, args.deployed, args.ssh, args.commit))


if __name__ == '__main__':
    main()
