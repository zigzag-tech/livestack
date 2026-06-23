"""polycore-specific behaviour: the Coordinator seam, residency metadata, and the
load/unload primitives — none of which require a GPU."""
from __future__ import annotations

import time
import unittest

import polycore
from polycore import (ManagedUnit, ModelManager, ResidencyPolicy,
                      Coordinator, LocalCoordinator)


class Backend:
    """Counts loads/frees so tests can assert the manager loads once and frees on evict."""
    def __init__(self):
        self.loads: dict[str, int] = {}
        self.frees = 0

    def loader(self, name):
        def _l():
            self.loads[name] = self.loads.get(name, 0) + 1
            return f"model::{name}"
        return _l

    def freer(self):
        self.frees += 1


def _mgr(coload=True, idle=0, coordinator=None, **unit_kw):
    be = Backend()
    units = {
        "asr": ManagedUnit("asr", be.loader("asr"), be.freer,
                           residency_policy=ResidencyPolicy.HARD_PIN, min_resident=1,
                           footprint=4_000_000_000),
        "tts": ManagedUnit("tts", be.loader("tts"), be.freer,
                           residency_policy=ResidencyPolicy.SOFT_PIN),
        "align": ManagedUnit("align", be.loader("align"), be.freer),
    }
    m = ModelManager(units, idle_seconds=idle, coload=coload,
                     coordinator=coordinator, log=lambda *_: None)
    return m, be


class ManagerBehaviour(unittest.TestCase):

    def test_ensure_loads_once_then_shares(self):
        m, be = _mgr()
        a = m.ensure("asr")
        b = m.ensure("asr")
        self.assertIs(a, b)
        self.assertEqual(be.loads["asr"], 1)        # second ensure does not reload
        self.assertEqual(sorted(m.resident), ["asr"])

    def test_coload_keeps_both(self):
        m, _ = _mgr(coload=True)
        m.ensure("asr"); m.ensure("tts")
        self.assertEqual(sorted(m.resident), ["asr", "tts"])

    def test_no_coload_evicts_and_frees(self):
        m, be = _mgr(coload=False)
        m.ensure("asr")
        m.ensure("tts")                              # evicts asr
        self.assertEqual(sorted(m.resident), ["tts"])
        self.assertEqual(be.frees, 1)

    def test_unload_now_empties_and_is_sorted(self):
        m, _ = _mgr()
        m.ensure("tts"); m.ensure("asr")
        self.assertEqual(m.unload_now(), ["asr", "tts"])
        self.assertEqual(m.resident, set())
        self.assertEqual(m.unload_now(), [])         # idempotent on empty

    def test_idle_evict(self):
        m, _ = _mgr(idle=1)
        m.ensure("asr")
        m.last_used = time.monotonic() - 5
        self.assertTrue(m.maybe_evict())
        self.assertEqual(m.resident, set())

    def test_touch_blocks_idle_evict(self):
        m, _ = _mgr(idle=1)
        m.ensure("asr")
        m.last_used = time.monotonic() - 5
        m.touch()                                    # resets timer
        self.assertFalse(m.maybe_evict())
        self.assertEqual(sorted(m.resident), ["asr"])

    def test_status_shape(self):
        m, _ = _mgr(coload=True, idle=30)
        m.ensure("asr")
        st = m.status()
        self.assertEqual(set(st), {"resident", "coload", "idle_seconds", "idle_for", "units"})
        self.assertEqual(st["resident"], ["asr"])
        self.assertEqual(st["coload"], True)
        self.assertEqual(st["idle_seconds"], 30)
        self.assertEqual(sorted(st["units"]), ["align", "asr", "tts"])

    def test_residency_metadata_preserved(self):
        m, _ = _mgr()
        self.assertEqual(m.units["asr"].residency_policy, ResidencyPolicy.HARD_PIN)
        self.assertEqual(m.units["asr"].min_resident, 1)
        self.assertEqual(m.units["asr"].footprint, 4_000_000_000)
        self.assertEqual(m.units["tts"].residency_policy, ResidencyPolicy.SOFT_PIN)
        self.assertEqual(m.units["align"].residency_policy, ResidencyPolicy.UNPINNED)

    def test_ensure_unknown_raises(self):
        m, _ = _mgr()
        with self.assertRaises(KeyError):
            m.ensure("ghost")


class SeamContract(unittest.TestCase):

    def test_localcoordinator_is_a_coordinator(self):
        self.assertIsInstance(LocalCoordinator(), Coordinator)

    def test_on_evict_request_unloads_resident_unit(self):
        m, be = _mgr(coload=True)
        m.ensure("asr"); m.ensure("tts")
        m.coordinator.on_evict_request("asr")        # simulate a broker evict command
        self.assertEqual(sorted(m.resident), ["tts"])
        self.assertEqual(be.frees, 1)

    def test_custom_coordinator_drives_loads(self):
        """A custom Coordinator can override policy and still use manager primitives."""
        events = []

        class RecordingCoordinator(LocalCoordinator):
            def acquire(self, name):
                events.append(("acquire", name))
                return super().acquire(name)

            def report_busy(self, name, busy):
                events.append(("busy", name, busy))

        m, _ = _mgr(coordinator=RecordingCoordinator(coload=True))
        m.ensure("asr")
        m.coordinator.report_busy("asr", True)
        self.assertIn(("acquire", "asr"), events)
        self.assertIn(("busy", "asr", True), events)


if __name__ == "__main__":
    unittest.main()
