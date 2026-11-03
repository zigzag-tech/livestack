"""The liveness rule against its shared vectors (the ZZOPS Node client runs a copy of the same file)."""
import json
from pathlib import Path

import pytest

from livestack_node.workloads import liveness
from livestack_node.workloads.liveness import BlockSizer, Liveness

VECTORS = json.loads((Path(__file__).parent/'fixtures'/'transfer_liveness_vectors.json').read_text())


def test_constants_match_the_contract():
    c = VECTORS['constants']
    live = Liveness()
    assert (live.stall_seconds, live.ceiling_minimum, live.floor_rate, live.slow_rate, live.max_block, live.min_block) == (
        c['stall_seconds'], c['ceiling_minimum'], c['floor_rate'], c['slow_rate'], c['max_block'], c['min_block'])
    assert (liveness.QUANTUM, liveness.EWMA_ALPHA) == (c['quantum'], c['ewma_alpha'])


@pytest.mark.parametrize('case', VECTORS['ceiling'], ids=lambda c: c['name'])
def test_ceiling(case):
    assert Liveness().ceiling(case['nbytes'], case['minimum']) == case['expect']


@pytest.mark.parametrize('case', VECTORS['verdict'], ids=lambda c: c['name'])
def test_verdict(case):
    assert Liveness().verdict(case['idle'], case['elapsed'], case['nbytes']) == case['expect']


@pytest.mark.parametrize('case', VECTORS['sizer'], ids=lambda c: c['name'])
def test_block_sizer(case):
    sizer = BlockSizer(Liveness())
    for step in case['steps']:
        sizer.failed() if 'fail' in step else sizer.ok(*step['ok'])
        assert sizer.block == step['block'], step


def test_overrides_are_validated():
    with pytest.raises(ValueError, match='unknown'):
        Liveness(stall=3)
    with pytest.raises(ValueError, match='positive'):
        Liveness(stall_seconds=0)
    with pytest.raises(ValueError, match='min_block'):
        Liveness(min_block=8*1024*1024)
