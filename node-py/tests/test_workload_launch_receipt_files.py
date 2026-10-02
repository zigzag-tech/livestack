"""Real filesystem bounds at the current launch-receipt writer boundary."""
import json
from pathlib import Path

import pytest

from livestack_node.workloads.compilation_policy import CLASSES
from livestack_node.workloads.launch_guard import write_current_receipt
from livestack_node.workloads.model import WorkloadError


def test_repeated_current_receipts_have_fixed_count_and_zero_byte_locks(tmp_path):
    for phase in range(20):
        for kind in CLASSES:
            write_current_receipt(tmp_path,kind,dict(version=1,phase=phase))
    assert len(list(tmp_path.iterdir()))==2*len(CLASSES)
    for kind in CLASSES:
        receipt=tmp_path/('compilation-'+kind+'.json')
        assert receipt.stat().st_size<=16384
        assert json.loads(receipt.read_bytes())==dict(version=1,phase=19)
        assert receipt.with_suffix('.lock').stat().st_size==0


@pytest.mark.parametrize('entry', ['lock-symlink','lock-hardlink','lock-bytes','lock-fifo',
                                   'staging-symlink','staging-bytes'])
def test_unsafe_receipt_entries_refuse_without_overwriting_evidence(tmp_path,entry):
    receipt=tmp_path/'compilation-rust.json'
    write_current_receipt(tmp_path,'rust',dict(version=1,admitted=True))
    original=receipt.read_bytes()
    sentinel=tmp_path/'sentinel'
    sentinel_bytes=b'' if entry=='lock-hardlink' else b'preserve'
    sentinel.write_bytes(sentinel_bytes)
    lock=receipt.with_suffix('.lock');temporary=receipt.with_suffix('.tmp')
    if entry.startswith('lock-'):
        lock.unlink()
        if entry=='lock-symlink':lock.symlink_to(sentinel)
        elif entry=='lock-hardlink':lock.hardlink_to(sentinel)
        elif entry=='lock-fifo':
            import os
            os.mkfifo(lock)
        else:lock.write_bytes(b'x')
    elif entry=='staging-symlink':temporary.symlink_to(sentinel)
    else:temporary.write_bytes(b'x'*16385)
    with pytest.raises(WorkloadError,match='compilation_launch_receipt_(lock|staging)_invalid'):
        write_current_receipt(tmp_path,'rust',dict(version=1,admitted=False))
    assert receipt.read_bytes()==original
    assert sentinel.read_bytes()==sentinel_bytes


def test_unknown_class_cannot_add_receipt_history(tmp_path):
    with pytest.raises(WorkloadError,match='compilation_launch_class_invalid'):
        write_current_receipt(tmp_path,'arbitrary-phase',dict(version=1))
    assert not list(tmp_path.iterdir())


def test_oversized_receipt_refuses_before_creating_files(tmp_path):
    with pytest.raises(WorkloadError):
        write_current_receipt(tmp_path,'rust',dict(padding='x'*16384))
    assert not list(tmp_path.iterdir())
