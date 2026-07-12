"""Parity: polycore.ModelManager + LocalCoordinator must evolve residency IDENTICALLY
to polyasr's existing AsrModelManager across the same op sequences. This is the proof
that Phase-0's drop-in replacement is behaviour-preserving.

Skipped automatically if ~/polyasr/polyasr_manager.py isn't present.
"""
from __future__ import annotations

import importlib.util
import os
import time
import unittest

import livestack_node as polycore
def _load_ref():
    path = os.path.expanduser("~/polyasr/polyasr_manager.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("polyasr_manager_ref", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REF = _load_ref()


def _fakes():
    """A loader that returns a distinct sentinel per unit and a no-op freer."""
    def make(name):
        return lambda: f"model::{name}"
    return make


def _status_no_time(st: dict) -> dict:
    st = dict(st)
    st.pop("idle_for", None)
    return st


@unittest.skipUnless(REF is not None, "polyasr_manager.py not available")
class LocalParity(unittest.TestCase):
    UNITS = ("asr", "align", "diarize")

    def _build(self, coload: bool, idle: int):
        mk = _fakes()
        ref = REF.AsrModelManager(
            units={n: REF.ManagedUnit(n, mk(n), REF.trim_ram) for n in self.UNITS},
            idle_seconds=idle, coload=coload,
        )
        new = polycore.ModelManager(
            units={n: polycore.ManagedUnit(n, mk(n), polycore.trim_ram) for n in self.UNITS},
            idle_seconds=idle, coload=coload, log=lambda *_: None,
        )
        return ref, new

    def _assert_same(self, ref, new):
        self.assertEqual(sorted(ref.resident), sorted(new.resident))
        self.assertEqual(_status_no_time(ref.status()), _status_no_time(new.status()))

    def _run_seq(self, ref, new, ops):
        for op in ops:
            rr = op(ref)
            nr = op(new)
            self.assertEqual(rr, nr, f"return mismatch on {op}")
            self._assert_same(ref, new)

    def test_coload_on_keeps_siblings(self):
        ref, new = self._build(coload=True, idle=0)
        self._run_seq(ref, new, [
            lambda m: m.ensure("asr"),
            lambda m: m.ensure("align"),
            lambda m: m.ensure("diarize"),
            lambda m: m.ensure("asr"),          # already resident, no reload
        ])
        self.assertEqual(sorted(new.resident), ["align", "asr", "diarize"])

    def test_coload_off_evicts_siblings(self):
        ref, new = self._build(coload=False, idle=0)
        self._run_seq(ref, new, [
            lambda m: m.ensure("asr"),
            lambda m: m.ensure("align"),         # evicts asr
            lambda m: m.ensure("diarize"),       # evicts align
            lambda m: m.ensure("asr"),           # evicts diarize
        ])
        self.assertEqual(sorted(new.resident), ["asr"])

    def test_unload_now_parity(self):
        ref, new = self._build(coload=True, idle=0)
        for m in (ref, new):
            m.ensure("asr"); m.ensure("align")
        self.assertEqual(ref.unload_now(), new.unload_now())
        self._assert_same(ref, new)
        # second unload on empty returns [] for both
        self.assertEqual(ref.unload_now(), new.unload_now())

    def test_idle_evict_parity(self):
        ref, new = self._build(coload=True, idle=1)
        for m in (ref, new):
            m.ensure("asr"); m.ensure("diarize")
            m.last_used = time.monotonic() - 1000   # force past idle
        self.assertTrue(ref.maybe_evict())
        self.assertTrue(new.maybe_evict())
        self._assert_same(ref, new)

    def test_idle_evict_disabled_parity(self):
        ref, new = self._build(coload=True, idle=0)   # 0 => never evict
        for m in (ref, new):
            m.ensure("asr"); m.last_used = time.monotonic() - 1000
        self.assertFalse(ref.maybe_evict())
        self.assertFalse(new.maybe_evict())
        self._assert_same(ref, new)

    def test_unknown_unit_raises_both(self):
        ref, new = self._build(coload=True, idle=0)
        with self.assertRaises(KeyError):
            ref.ensure("nope")
        with self.assertRaises(KeyError):
            new.ensure("nope")


if __name__ == "__main__":
    unittest.main()
