"""polycore-specific behaviour: the Coordinator seam, residency metadata, and the
load/unload primitives — none of which require a GPU."""
from __future__ import annotations

import time
import unittest

import livestack_node as polycore
from livestack_node import (ManagedUnit, ModelManager, ResidencyPolicy,
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


class FunctionalHealth(unittest.TestCase):
    """A resident unit can be process-alive yet functionally degraded (the ASR
    silent-empty-partials bug). The manager verifies a unit's own probe and
    evicts+reloads the degraded ones, surfacing them to the coordinator."""

    def _mgr_with_probe(self, healthy_flag, coordinator=None):
        be = Backend()
        units = {
            "asr": ManagedUnit("asr", be.loader("asr"), be.freer,
                               residency_policy=ResidencyPolicy.HARD_PIN,
                               health_check=lambda _model: healthy_flag["asr"]),
            "tts": ManagedUnit("tts", be.loader("tts"), be.freer),  # no probe
        }
        m = ModelManager(units, idle_seconds=0, coload=True,
                         coordinator=coordinator, log=lambda *_: None)
        return m, be

    def test_check_health_none_without_probe_or_when_unloaded(self):
        flag = {"asr": True}
        m, _ = self._mgr_with_probe(flag)
        self.assertIsNone(m.units["asr"].check_health())   # has probe, not loaded
        self.assertIsNone(m.units["tts"].check_health())   # no probe
        m.ensure("tts")
        self.assertIsNone(m.units["tts"].check_health())   # loaded but no probe

    def test_healthy_unit_is_not_reloaded(self):
        flag = {"asr": True}
        m, be = self._mgr_with_probe(flag)
        m.ensure("asr")
        self.assertEqual(m.maybe_recover_degraded(), [])
        self.assertEqual(be.loads["asr"], 1)               # not reloaded
        self.assertEqual(be.frees, 0)

    def test_degraded_unit_is_evicted_and_reloaded(self):
        flag = {"asr": True}
        m, be = self._mgr_with_probe(flag)
        m.ensure("asr")
        flag["asr"] = False                                # partial path goes silent
        self.assertEqual(m.maybe_recover_degraded(), ["asr"])
        self.assertEqual(be.loads["asr"], 2)               # reloaded once
        self.assertEqual(be.frees, 1)                      # old instance freed
        self.assertIn("asr", m.resident)                   # warm again

    def test_recover_is_rate_limited(self):
        flag = {"asr": False}
        m, be = self._mgr_with_probe(flag)
        m.ensure("asr")                                    # loads=1
        self.assertEqual(m.maybe_recover_degraded(min_interval=600), ["asr"])  # loads=2
        # Still unhealthy, but within the interval → must not hot-loop.
        self.assertEqual(m.maybe_recover_degraded(min_interval=600), [])
        self.assertEqual(be.loads["asr"], 2)

    def test_probe_that_raises_counts_as_degraded(self):
        def boom(_model):
            raise RuntimeError("probe blew up")
        be = Backend()
        units = {"asr": ManagedUnit("asr", be.loader("asr"), be.freer,
                                    health_check=boom)}
        m = ModelManager(units, idle_seconds=0, log=lambda *_: None)
        m.ensure("asr")
        self.assertEqual(m.maybe_recover_degraded(), ["asr"])
        self.assertEqual(be.loads["asr"], 2)

    def test_explicit_recover_notifies_coordinator(self):
        events = []

        class RecordingCoordinator(LocalCoordinator):
            def on_degraded(self, name):
                events.append(("degraded", name))

        flag = {"asr": True}
        m, be = self._mgr_with_probe(flag, coordinator=RecordingCoordinator(coload=True))
        m.ensure("asr")
        m.recover("asr")
        self.assertEqual(be.loads["asr"], 2)
        self.assertIn(("degraded", "asr"), events)

    def test_sweep_notifies_coordinator_on_degraded(self):
        events = []

        class RecordingCoordinator(LocalCoordinator):
            def on_degraded(self, name):
                events.append(name)

        flag = {"asr": False}
        m, _ = self._mgr_with_probe(flag, coordinator=RecordingCoordinator(coload=True))
        m.ensure("asr")
        m.maybe_recover_degraded()
        self.assertEqual(events, ["asr"])

    def test_localcoordinator_still_satisfies_protocol(self):
        # on_degraded added to the Protocol; LocalCoordinator must still match.
        self.assertIsInstance(LocalCoordinator(), Coordinator)


if __name__ == "__main__":
    unittest.main()


def test_run_scope_brackets_observer_around_op():
    """run_scope() must call observer.begin before the body and observer.end after —
    including when the body raises (finally), so a failed op still records its peak."""
    import livestack_node as ln
    calls = []

    class _Obs:
        def begin(self, u): calls.append(("begin", u))
        def end(self, u): calls.append(("end", u))

    units = {"a": ln.ManagedUnit("a", lambda: "MODEL", ln.noop_free, footprint=1)}
    m = ln.ModelManager(units, idle_seconds=0, activation_observer=_Obs())
    with m.run_scope("a") as model:
        assert model == "MODEL"
        calls.append(("body", "a"))
    assert calls == [("begin", "a"), ("body", "a"), ("end", "a")]

    calls.clear()
    try:
        with m.run_scope("a"):
            raise ValueError("boom")
    except ValueError:
        pass
    assert calls == [("begin", "a"), ("end", "a")]


def test_footprint_accepts_a_resource_vector():
    """A Strata-style unit pins host RAM as well as VRAM: `footprint` is a
    VECTOR ({"vram_bytes": N, "ram_bytes": M}). The Rust residency core, whose
    budget is VRAM alone, is fed `vram_bytes` exactly as before — so a unit
    declaring a vector plans like the int unit it replaced."""
    import livestack_node as ln
    be = Backend()
    unit = ln.ManagedUnit("flash_next", be.loader("flash_next"), be.freer,
                          footprint={"vram_bytes": 20, "ram_bytes": 45},
                          exclusive_device=True,
                          engine="strata", engine_rev="36fa455")
    assert unit.vram_bytes == 20
    m = ln.ModelManager({"flash_next": unit}, idle_seconds=0, coload=True,
                        log=lambda *_: None)
    assert m.ensure("flash_next") == "model::flash_next"
    assert m.resident == {"flash_next"}
    assert unit.exclusive_device is True
    assert (unit.engine, unit.engine_rev) == ("strata", "36fa455")
    # Every consumer of `footprint` speaks the vector (2026-10-02: serve.py's
    # activation-store signature int()-crashed on the dict and the node would
    # not boot). The signature changes when EITHER dimension changes.
    from livestack_node.serve import _footprint_signature
    assert _footprint_signature({"flash_next": unit})
    vec2 = ln.ManagedUnit("flash_next", be.loader("flash_next"), be.freer,
                          footprint={"vram_bytes": 20, "ram_bytes": 46})
    assert _footprint_signature({"flash_next": unit}) != \
        _footprint_signature({"flash_next": vec2})
    plain = ln.ManagedUnit("a", be.loader("a"), be.freer, footprint=20)
    assert _footprint_signature({"a": plain})   # the int form still works


def test_exclusive_overrides_coload_acquiring_one_evicts_its_siblings():
    """A unit declaring `exclusive_device` claims the DEVICE, not a slot beside
    its siblings: acquiring it evicts the process's other units even under
    coload=True. coload says "siblings may stay"; it cannot make room the unit's
    own declaration says must be empty — an engine that sizes its cache to the
    free VRAM will not start into a busy card at all (measured 2026-10-02:
    Strata saw 399 MiB free and its cudaMalloc failed). It is also what keeps
    the "broker temporarily forgot" local fallback safe: that path loads without
    the planner, and this is the only room-making left."""
    import livestack_node as ln
    from livestack_node.coordinator import LivestackCoordinator
    be = Backend()
    units = {
        "llm_general": ln.ManagedUnit("llm_general", be.loader("llm_general"), be.freer,
                                      footprint=21),
        "flash_next": ln.ManagedUnit("flash_next", be.loader("flash_next"), be.freer,
                                     footprint={"vram_bytes": 20, "ram_bytes": 45},
                                     exclusive_device=True),
    }
    m = ln.ModelManager(units, idle_seconds=0, coload=True, log=lambda *_: None)
    coord = LivestackCoordinator("h", coload=True)
    coord.bind(m)
    m.ensure("llm_general")
    assert m.resident == {"llm_general"}
    coord.acquire("flash_next")          # coload=True — and it still evicts
    assert m.resident == {"flash_next"}
    # A NON-exclusive unit under coload=True keeps its siblings (unchanged).
    units["embed"] = ln.ManagedUnit("embed", be.loader("embed"), be.freer, footprint=2)
    m2 = ln.ModelManager({"llm_general": units["llm_general"], "embed": units["embed"]},
                         idle_seconds=0, coload=True, log=lambda *_: None)
    coord2 = LivestackCoordinator("h", coload=True)
    coord2.bind(m2)
    m2.ensure("llm_general")
    coord2.acquire("embed")
    assert m2.resident == {"llm_general", "embed"}


def test_the_exclusive_claim_is_symmetric_nothing_co_places_with_it():
    """Acquiring a PLAIN unit while an exclusive one is resident evicts the
    exclusive tenant first: its claim means nobody co-places beside it. The
    converse (exclusive acquirer evicts siblings) is the previous test.
    Without this, a named `local` request loaded the 27B beside a resident
    Flash-Next — two tenants the planner models as impossible — and the world
    read the card massively over-budget until step 0 shed in a loop (ledger:
    "relieve measured over-budget pressure" every ~10 s, 2026-10-02)."""
    import livestack_node as ln
    from livestack_node.coordinator import LivestackCoordinator
    be = Backend()
    units = {
        "flash_next": ln.ManagedUnit("flash_next", be.loader("flash_next"), be.freer,
                                     footprint={"vram_bytes": 20, "ram_bytes": 45},
                                     exclusive_device=True),
        "llm_general": ln.ManagedUnit("llm_general", be.loader("llm_general"), be.freer,
                                      footprint=21),
    }
    m = ln.ModelManager(units, idle_seconds=0, coload=True, log=lambda *_: None)
    coord = LivestackCoordinator("h", coload=True)
    coord.bind(m)
    m.ensure("flash_next")
    assert m.resident == {"flash_next"}
    coord.acquire("llm_general")     # plain unit, coload=True — and the
    assert m.resident == {"llm_general"}   # exclusive tenant still leaves


def test_a_busy_tenant_is_never_evicted_out_from_under_its_work():
    """Idle-only preemption applies to the manager's local acquire exactly as
    it does to the planner's ("a busy one defers the admission"): an exclusive
    acquire that would evict a BUSY co-tenant REFUSES instead of cutting a
    live stream (measured 2026-10-02: title traffic bouncing the models cut a
    26k-token generation with ReadError)."""
    import livestack_node as ln
    from livestack_node.coordinator import LivestackCoordinator
    be = Backend()
    units = {
        "llm_general": ln.ManagedUnit("llm_general", be.loader("llm_general"), be.freer,
                                      footprint=21),
        "flash_next": ln.ManagedUnit("flash_next", be.loader("flash_next"), be.freer,
                                     footprint={"vram_bytes": 20, "ram_bytes": 45},
                                     exclusive_device=True),
    }
    m = ln.ModelManager(units, idle_seconds=0, coload=True, log=lambda *_: None)
    coord = LivestackCoordinator("h", coload=True)
    coord.bind(m)
    m.ensure("llm_general")
    m.units["llm_general"].busy = True   # a request is in flight on the 27B
    try:
        import pytest
        with pytest.raises(RuntimeError, match="busy"):
            coord.acquire("flash_next")
    finally:
        m.units["llm_general"].busy = False
    assert m.resident == {"llm_general"}      # the stream survived
    coord.acquire("flash_next")               # idle now: the claim applies
    assert m.resident == {"flash_next"}
