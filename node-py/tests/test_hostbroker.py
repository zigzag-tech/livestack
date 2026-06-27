"""HostBroker: cross-process preemption on one shared GPU (the real single-host
case — polyasr/polytts/chipgen are separate processes). Fake peers stand in for the
three servers; we assert the broker dispatches the right warm/evict calls."""
from livestack_node.measure import measure_footprint
from livestack_node.hostbroker import HostBroker
from livestack_node.planner import Device, Unit, Placement, Request, Residency


class FakePeer:
    def __init__(self, host, device, unit, resident=False, busy=False):
        self.host_id = host
        self.device_id = device
        self._unit = unit
        self._resident = resident
        self._busy = busy
        self.calls = []

    def units(self):
        return {self._unit.kind: self._unit}

    def placements(self):
        if not self._resident:
            return []
        return [Placement(self._unit.kind, self.device_id, loaded_at=0, busy=self._busy)]

    def warm(self, kind):
        self.calls.append(("warm", kind)); self._resident = True

    def evict(self, kind):
        self.calls.append(("evict", kind)); self._resident = False


def make_host(tts_busy=False, chip_busy=False):
    dev = Device("gpu0", "tower0", capacity={"vram": 24}, reserved={"vram": 1})
    asr = FakePeer("tower0", "gpu0",
                   Unit("align", {"vram": 10}, priority=10, residency=Residency.HARD_PIN,
                        min_resident=1, reload_cost=8))
    tts = FakePeer("tower0", "gpu0",
                   Unit("tts", {"vram": 9}, priority=20, residency=Residency.SOFT_PIN,
                        reload_cost=6),
                   resident=True, busy=tts_busy)
    chip = FakePeer("tower0", "gpu0",
                    Unit("chipgen", {"vram": 5}, priority=30, residency=Residency.UNPINNED,
                         reload_cost=4),
                    resident=True, busy=chip_busy)
    return HostBroker([dev], [asr, tts, chip], clock=lambda: 1000.0), asr, tts, chip


def test_align_request_preempts_idle_chipgen_in_other_process():
    broker, asr, tts, chip = make_host()
    dev = broker.admit(Request("r1", "align", created_at=1000))
    assert dev == "gpu0"
    assert ("evict", "chipgen") in chip.calls    # broker told the chipgen PROCESS to evict
    assert ("warm", "align") in asr.calls        # and the asr process to warm
    assert ("evict", "tts") not in tts.calls     # more-important TTS left alone


def test_align_defers_when_lower_priority_all_busy():
    broker, asr, tts, chip = make_host(tts_busy=True, chip_busy=True)
    dev = broker.admit(Request("r1", "align", created_at=1000))
    assert dev is None                            # 时间换空间: wait, don't interrupt busy work
    assert ("evict", "chipgen") not in chip.calls
    assert ("evict", "tts") not in tts.calls


def test_measure_footprint_captures_peak_activation():
    # weights 10 GB but a transient 12 GB peak during a run -> footprint = 12 GB.
    class M:
        def __init__(self): self._a = 0; self._p = 0
        def reset_peak(self): self._p = self._a
        def allocated(self): return self._a
        def max_allocated(self): return self._p
    m = M()

    def load():
        m._a = 10_000_000_000; m._p = 10_000_000_000; return "model"

    def run(model):
        m._p = 12_000_000_000        # activation high-water mark

    model, fp = measure_footprint(load, run, meter=m)
    assert model == "model"
    assert fp["vram_bytes"] == 12_000_000_000
