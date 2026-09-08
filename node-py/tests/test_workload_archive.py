"""Source identity checked against real files, modes and malicious tar members."""
import hashlib
from io import BytesIO
from pathlib import Path
import tarfile

import pytest

from livestack_node.workloads.archive import capture, file_digest, unpack
from livestack_node.workloads.model import WorkloadError


def test_private_snapshot_is_stable_and_preserves_executability(tmp_path):
    root = tmp_path/'source'
    root.mkdir()
    (root/'main.py').write_text('print("captured")\n')
    (root/'main.py').chmod(0o755)
    a = capture(root, ['main.py'], tmp_path/'a.tar', provenance={'commit': '123'})
    b = capture(root, ['main.py'], tmp_path/'b.tar', provenance={'commit': '123'})
    assert a['digest'] == b['digest']
    (root/'main.py').write_text('print("changed")\n')
    unpack(tmp_path/'a.tar', tmp_path/'private', a['digest'])
    assert (tmp_path/'private/main.py').read_text() == 'print("captured")\n'
    assert (tmp_path/'private/main.py').stat().st_mode & 0o111


def test_symlink_and_traversal_capture_are_refused(tmp_path):
    root = tmp_path/'source'
    root.mkdir()
    (tmp_path/'outside').write_text('private')
    (root/'external').symlink_to(tmp_path/'outside')
    for path in ['external', '../outside', '/outside']:
        with pytest.raises(WorkloadError):
            capture(root, [path], tmp_path/'a.tar')


@pytest.mark.parametrize('name,kind', [('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
                                     ('symlink', tarfile.SYMTYPE), ('device', tarfile.CHRTYPE)])
def test_unsafe_archive_members_cannot_escape(tmp_path, name, kind):
    bundle = tmp_path/'unsafe.tar'
    with tarfile.open(bundle, 'w') as archive:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = '/etc/passwd'
        info.size = 0
        archive.addfile(info, BytesIO())
    with pytest.raises(WorkloadError):
        unpack(bundle, tmp_path/'private', file_digest(bundle))
    assert not (tmp_path/'private').exists()
    assert not (tmp_path/'escape').exists()


def test_wrong_digest_never_publishes_directory(tmp_path):
    root = tmp_path/'source'
    root.mkdir()
    (root/'file').write_bytes(b'input')
    capture(root, ['file'], tmp_path/'a.tar')
    with pytest.raises(WorkloadError, match='digest'):
        unpack(tmp_path/'a.tar', tmp_path/'private', '0'*64)
    assert not (tmp_path/'private').exists()
