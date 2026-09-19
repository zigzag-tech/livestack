"""Warming happens after the bind, because arbitration cannot grant to a node
it has never heard of.

The deadlock, measured on xc-mac-studio 2026-09-18: polytts loaded its model
inside the web framework's startup hook, so the port never bound; the broker
could not snapshot it, so it never learned the node hosts `polytts`; and
`admit` for an unknown kind is a refusal by design. The process sat with no
listener, no sockets and no CPU, and the fleet showed `mia` /
`Connection refused` for forty-six hours.
"""
import threading

from livestack_node.serve import _start_preload


class FakeManager:
    def __init__(self):
        self.ensured = []

    def ensure(self, name):
        self.ensured.append(name)
        return f"model:{name}"


def run_sync(fn):
    """`gpu_call` in tests: run it here, so ordering is deterministic."""
    return fn()


def drain(thread, timeout=5.0):
    thread.join(timeout)
    assert not thread.is_alive(), "preload thread did not finish"


def test_it_waits_for_the_facade_before_warming():
    manager = FakeManager()
    answered = []
    # Not answering the first two times: a server that is still binding.
    answers = lambda url: (answered.append(url), len(answered) >= 3)[1]

    t = _start_preload("qwen", manager=manager, gpu_call=run_sync,
                       facade_url="http://127.0.0.1:8100/livestack",
                       answers=answers, log=lambda m: None, sleep=lambda s: None)
    drain(t)
    assert manager.ensured == ["qwen"]
    assert len(answered) == 3


def test_it_gives_up_rather_than_spinning_on_a_door_that_never_opens():
    manager = FakeManager()
    logged = []
    t = _start_preload("qwen", manager=manager, gpu_call=run_sync,
                       facade_url="http://127.0.0.1:8100/livestack",
                       answers=lambda url: False, log=logged.append,
                       sleep=lambda s: None, attempts=3)
    drain(t)
    assert manager.ensured == []
    assert any("never answered" in m for m in logged)


def test_several_units_warm_in_order():
    manager = FakeManager()
    t = _start_preload(["qwen", "voxcpm"], manager=manager, gpu_call=run_sync,
                       facade_url=None, log=lambda m: None, sleep=lambda s: None)
    drain(t)
    assert manager.ensured == ["qwen", "voxcpm"]


def test_a_callable_is_warmed_as_itself():
    # A node whose warm is more than "ensure one unit" — voices, caches — hands
    # a thunk instead of a name.
    manager = FakeManager()
    ran = []
    t = _start_preload(lambda: ran.append(True), manager=manager, gpu_call=run_sync,
                       facade_url=None, log=lambda m: None, sleep=lambda s: None)
    drain(t)
    assert ran == [True]
    assert manager.ensured == []


def test_a_failed_warm_leaves_the_node_up():
    # A node that cannot warm reports `ready: false` and is not chosen. One
    # that exits takes with it the endpoints that would have said why.
    class Angry(FakeManager):
        def ensure(self, name):
            raise RuntimeError("the planner could not place it on any device")

    logged = []
    t = _start_preload(["qwen", "voxcpm"], manager=Angry(), gpu_call=run_sync,
                       facade_url=None, log=logged.append, sleep=lambda s: None)
    drain(t)
    # And it keeps going: the second unit is still attempted.
    assert sum("failed" in m for m in logged) == 2


def test_it_does_not_hold_the_caller():
    # attach() returns immediately; the whole point is that the server binds.
    slow = threading.Event()
    manager = FakeManager()
    t = _start_preload(lambda: slow.wait(2.0), manager=manager, gpu_call=run_sync,
                       facade_url=None, log=lambda m: None, sleep=lambda s: None)
    assert t.is_alive()
    slow.set()
    drain(t)


def test_a_callable_is_not_marshalled_through_the_nodes_gpu_executor():
    """The second deadlock, measured on xc-tower-ubuntu 2026-09-18.

    A node's GPU executor is normally ONE worker — Metal thread affinity and
    MPS both require it — and a node's own warm thunk marshals to that same
    executor. Wrapping the thunk in `gpu_call` occupies the one worker; the
    thunk then submits to the same pool and waits for a worker that will never
    come. polytts hung on its first load, nothing became resident, and every
    /tts afterwards queued behind the wedged worker: a TTS node answering
    /health with 200 and synthesizing nothing.
    """
    import concurrent.futures

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        loaded = []

        def gpu_call(fn):
            return pool.submit(fn).result()

        # Exactly polytts's shape: the caller's thunk marshals for itself.
        def warm():
            return pool.submit(lambda: loaded.append("voxcpm")).result()

        t = _start_preload(warm, manager=FakeManager(), gpu_call=gpu_call,
                           facade_url=None, log=lambda m: None, sleep=lambda s: None)
        drain(t, timeout=5.0)
        assert loaded == ["voxcpm"]
    finally:
        pool.shutdown(wait=False)


def test_a_name_is_still_marshalled_because_the_framework_chose_where():
    # The other half of the rule: for a NAME, the framework decides where the
    # load runs, so it must use the node's own GPU call.
    manager = FakeManager()
    where = []
    t = _start_preload("qwen", manager=manager,
                       gpu_call=lambda fn: (where.append("gpu"), fn())[1],
                       facade_url=None, log=lambda m: None, sleep=lambda s: None)
    drain(t)
    assert manager.ensured == ["qwen"] and where == ["gpu"]
