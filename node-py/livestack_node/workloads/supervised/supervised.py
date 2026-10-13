"""Supervised handlers: slow work only through `ctx`, which reports for the handler.
Implements SPEC.md next to this file; supervised.mjs is the Node twin and vectors.json pins both.

This module is the ONLY place handler-side process, network and sleep primitives live
(tools/check-supervised-handlers.py forbids them in handler modules)."""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import urllib.request

from .. import lease_helper

DEFAULT_PERIOD = {'count': 6, 'seconds': 60}


class SilenceBreach(RuntimeError):
    """The handler was silent longer than its contract's max_silence_seconds (enforce mode)."""


# ---- pure reducer (SPEC.md "Reducer"); time is an argument, in integer milliseconds ---------------

def init(contract, t):
    period = contract.get('max_per_period') or DEFAULT_PERIOD
    return dict(contract=dict(contract), count=period['count'], seconds=period['seconds'],
                min_interval_ms=int(contract.get('min_interval_seconds', 1)*1000),
                doc={'phase': 'start'}, beat=0, emitted=None, last_event=t, last_emit=None,
                milli_base=period['count']*1000, base_t=t, dropped=0, pending=False)


def tokens(state, t):
    return min(state['count']*1000, state['milli_base'] + (t - state['base_t'])*state['count']//state['seconds'])


def step(state, event):
    """Apply one event; return the document to emit now, or None."""
    t, kind = event['t'], event['type']
    if kind == 'phase':
        state['doc'] = {'phase': event['name']}
    elif kind in ('done', 'tick'):
        if kind == 'done':
            state['doc'] = {k: v for k, v in state['doc'].items() if k not in ('done', 'total', 'unit')}
            state['doc'].update({k: event[k] for k in ('done', 'total', 'unit') if event.get(k) is not None})
        else:
            state['beat'] += 1
        if event.get('message') is not None:
            state['doc']['message'] = event['message']
    if kind != 'flush':
        state['last_event'] = t
    wanted = dict(state['doc'], **({'beat': state['beat']} if state['beat'] else {}))
    if wanted == state['emitted']:
        state['pending'] = False
        return None
    if tokens(state, t) >= 1000 and (state['last_emit'] is None or t - state['last_emit'] >= state['min_interval_ms']):
        state['milli_base'], state['base_t'] = tokens(state, t) - 1000, t
        state['last_emit'], state['emitted'], state['pending'] = t, wanted, False
        return wanted
    state['pending'] = True
    if kind != 'flush':
        state['dropped'] += 1
    return None


def verdict(state, t):
    return 'overdue' if t - state['last_event'] > state['contract']['max_silence_seconds']*1000 else 'ok'


# ---- ctx --------------------------------------------------------------------------------------

class Context:
    def __init__(self, contract, *, mode='report', clock=time.monotonic, backend=None, sink=None):
        self.mode, self._clock = mode, clock
        self._t0 = clock()
        self.state = init(contract, 0)
        self.backend = backend or ProcessBackend()
        self.sink = sink or lease_helper.write_progress
        self.lock = threading.RLock()
        self.breaches = 0
        self._breached = False

    def now_ms(self):
        return int((self._clock() - self._t0)*1000)

    def _event(self, kind, **fields):
        with self.lock:
            doc = step(self.state, dict(fields, t=self.now_ms(), type=kind))
            if doc is not None:
                self.sink(doc)

    def tick(self, message=None):
        self._event('tick', message=message)

    def flush(self):
        """Publish any held document the bucket now allows; called by long waits' poll loops."""
        self._event('flush')

    def guard(self):
        """Silence check. Report mode logs once per episode; enforce mode raises."""
        with self.lock:
            overdue = verdict(self.state, self.now_ms()) == 'overdue'
            if not overdue:
                self._breached = False
                return False
            if not self._breached:
                self._breached = True
                self.breaches += 1
                logging.warning('supervised handler silent for more than %ss (phase %s)',
                                self.state['contract']['max_silence_seconds'], self.state['doc'].get('phase'))
        if self.mode == 'enforce':
            raise SilenceBreach(f"silent for more than {self.state['contract']['max_silence_seconds']}s")
        return True

    def phase(self, name, fn):
        self._event('phase', name=name)
        return fn()

    def each(self, items, fn, unit='items'):
        items = list(items)
        self._event('done', done=0, total=len(items), unit=unit)
        results = []
        for index, item in enumerate(items, 1):
            results.append(fn(item))
            self._event('done', done=index, total=len(items), unit=unit)
        return results

    def exec(self, cmd, args=(), **options):
        return self.backend.exec(self, cmd, list(args), **options)

    def wait(self, awaitable, label):
        self._event('tick', message=label[:200])
        return self.backend.wait(self, awaitable, label)

    def download(self, url, dest):
        return self.backend.download(self, url, dest)

    def upload(self, url, path):
        return self.backend.upload(self, url, path)


def supervised(contract, *, mode=None, clock=time.monotonic, backend=None, sink=None):
    mode = mode or os.environ.get('HARMONY_SUPERVISED_MODE', 'report')

    def wrap(handler):
        def run(*args, **kwargs):
            ctx = Context(contract, mode=mode, clock=clock, backend=backend, sink=sink)
            try:
                return handler(ctx, *args, **kwargs)
            finally:
                with ctx.lock:  # the last held document is published regardless of the bucket
                    if ctx.state['pending']:
                        wanted = dict(ctx.state['doc'], **({'beat': ctx.state['beat']} if ctx.state['beat'] else {}))
                        ctx.sink(wanted)
                        ctx.state['emitted'], ctx.state['pending'] = wanted, False
                if ctx.state['dropped'] or ctx.breaches:
                    logging.info('supervised: %d reports coalesced, %d silence breaches',
                                 ctx.state['dropped'], ctx.breaches)
        run.contract = contract
        # The same handler under a different clock/backend/sink: how the conformance test drives it.
        run.rebuild = lambda **over: supervised(contract, **dict(dict(mode=mode, clock=clock, backend=backend, sink=sink), **over))(handler)
        return run
    return wrap


# ---- the real backend: the only raw primitives -----------------------------------------------

class ProcessBackend:
    POLL = 0.25
    TAIL = 64*1024

    def exec(self, ctx, cmd, args, *, env=None, cwd=None):
        child = subprocess.Popen([cmd, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env=env, cwd=cwd, bufsize=0)
        tail = bytearray()

        def pump():
            while True:
                chunk = child.stdout.read(8192)
                if not chunk:
                    return
                tail.extend(chunk)
                del tail[:-self.TAIL]
                ctx.tick()
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            while child.poll() is None:
                time.sleep(self.POLL)
                ctx.flush()
                ctx.guard()
        except SilenceBreach:
            child.kill()
            raise
        finally:
            if child.poll() is None:
                child.kill()
            reader.join(timeout=5)
            ctx.tick()
        return {'code': child.returncode, 'tail': bytes(tail)}

    def wait(self, ctx, awaitable, label):
        """`awaitable` is a callable or a concurrent.futures.Future; polled so flush/guard keep running."""
        box = {}
        if callable(awaitable):
            def run():
                try:
                    box['value'] = awaitable()
                except BaseException as error:
                    box['error'] = error
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            done = lambda: not worker.is_alive()
        else:
            done = awaitable.done
        while not done():
            time.sleep(self.POLL)
            ctx.flush()
            ctx.guard()
        if 'error' in box:
            raise box['error']
        return box['value'] if 'value' in box else awaitable.result()

    def download(self, ctx, url, dest):
        with urllib.request.urlopen(url) as response, open(dest, 'wb') as out:
            total = int(response.headers.get('Content-Length') or 0) or None
            done = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                ctx._event('done', done=min(done, total) if total else done, total=total, unit='bytes')
                ctx.guard()
        return done

    def upload(self, ctx, url, path):
        size = os.path.getsize(path)

        class Body:
            def __init__(self):
                self.file, self.sent = open(path, 'rb'), 0

            def read(self, n=65536):
                chunk = self.file.read(n)
                self.sent += len(chunk)
                ctx._event('done', done=self.sent, total=size, unit='bytes')
                ctx.guard()
                return chunk
        body = Body()
        request = urllib.request.Request(url, data=body, method='PUT', headers={'Content-Length': str(size)})
        with urllib.request.urlopen(request) as response:
            return response.status
