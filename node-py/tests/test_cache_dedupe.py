import os
import time

from livestack_node.workloads.cache_dedupe import dedupe


def put(path, data, age=3600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    t = time.time()-age
    os.utime(path, (t, t))


def test_identical_files_become_one_inode_and_different_ones_do_not(tmp_path):
    a, b = tmp_path/'w1'/'e1'/'big.bin', tmp_path/'w2'/'e9'/'big.bin'
    put(a, b'x'*100000); put(b, b'x'*100000)
    c, d = tmp_path/'w1'/'c.bin', tmp_path/'w2'/'c.bin'
    put(c, b'x'*99999+b'A'); put(d, b'x'*99999+b'B')
    put(tmp_path/'w1'/'small', b'tiny'); put(tmp_path/'w2'/'small', b'tiny')
    dry = dedupe([str(tmp_path)])
    assert dry['bytes_saved'] == 100000 and a.stat().st_ino != b.stat().st_ino, 'dry run changes nothing'
    done = dedupe([str(tmp_path)], apply=True)
    assert done['files_linked'] == 1
    assert a.stat().st_ino == b.stat().st_ino and b.read_bytes() == b'x'*100000
    assert c.stat().st_ino != d.stat().st_ino
    assert dedupe([str(tmp_path)], apply=True)['bytes_saved'] == 0, 'idempotent'
    assert not [p for p in tmp_path.rglob('*') if 'dedupe-' in p.name]


def test_fresh_files_symlinks_and_other_roots_untouched(tmp_path):
    inside, outside = tmp_path/'cache', tmp_path/'host-store'
    put(inside/'a', b'z'*70000, age=5); put(inside/'b', b'z'*70000, age=5)
    put(outside/'m', b'z'*70000); put(inside/'old', b'z'*70000)
    (inside/'link').symlink_to(outside/'m')
    r = dedupe([str(inside)], apply=True)
    assert r['files_linked'] == 0, 'young files are skipped; one old file has no partner inside the root'
    assert (outside/'m').stat().st_nlink == 1
