"""Provider-wide admission and physical drain; operator authorization is injected."""
from concurrent.futures import Executor
from threading import RLock
from uuid import uuid4


class FenceRefused(RuntimeError):
    pass


class ProviderFence:
    def __init__(self, authorize):
        self._authorize = authorize
        self._lock = RLock()
        self._holder = None
        self._epoch = 0
        self._active = set()
        self._uncertain = set()

    def hold(self, credential, holder):
        self._authorize(credential, 'hold')
        if not isinstance(holder, str) or not holder:
            raise FenceRefused('holder_required')
        with self._lock:
            if self._holder not in (None, holder):
                raise FenceRefused('fence_owned_by_another_holder')
            if self._holder is None:
                self._epoch += 1
                self._holder = holder
            return self._status()

    def status(self, credential):
        self._authorize(credential, 'status')
        with self._lock:
            return self._status()

    def _status(self):
        return {'holder': self._holder, 'epoch': self._epoch,
                'active': len(self._active), 'uncertain': len(self._uncertain),
                'drained': self._holder is not None and not self._active and not self._uncertain}

    def release(self, credential, holder, epoch):
        self._authorize(credential, 'release')
        with self._lock:
            if self._holder != holder or self._epoch != epoch:
                raise FenceRefused('fence_identity_mismatch')
            # Abort can reopen ingress with existing work; activation cannot use
            # this as proof of settlement. Sticky uncertainty remains visible.
            self._holder = None
            return self._status()

    def admit(self):
        with self._lock:
            if self._holder is not None:
                raise FenceRefused('provider_admission_held')
            key = uuid4().hex
            self._active.add(key)
            return key

    def settle(self, key, physically_settled=True):
        with self._lock:
            if key not in self._active:
                raise FenceRefused('unknown_admission')
            self._active.remove(key)
            if not physically_settled:
                self._uncertain.add(key)

    def executor(self, executor, synchronize):
        return FencedExecutor(self, executor, synchronize)


class FencedExecutor(Executor):
    """Track the underlying Future, never its cancellable asyncio wrapper."""
    def __init__(self, fence, executor, synchronize):
        self.fence, self.executor, self.synchronize = fence, executor, synchronize

    def submit(self, fn, /, *args, **kwargs):
        key = self.fence.admit()
        try:
            future = self.executor.submit(fn, *args, **kwargs)
        except BaseException:
            self.fence.settle(key)
            raise

        def finished(done):
            settled = True
            if not done.cancelled():
                try:
                    self.synchronize()
                except BaseException:
                    settled = False
            self.fence.settle(key, settled)
        future.add_done_callback(finished)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        return self.executor.shutdown(wait=wait, cancel_futures=cancel_futures)


class ProviderAdmissionMiddleware:
    """Track request/WS lifetime; worker tracking is independently mandatory.

    bypass is a closed owner-reviewed route predicate. It never supplies auth:
    existing endpoint owner-token checks execute unchanged.
    """
    def __init__(self, app, fence, bypass):
        self.app, self.fence, self.bypass = app, fence, bypass

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket') or self.bypass(scope):
            return await self.app(scope, receive, send)
        try:
            key = self.fence.admit()
        except FenceRefused:
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1013})
            else:
                await send({'type': 'http.response.start', 'status': 503,
                            'headers': [(b'content-type', b'application/json')]})
                await send({'type': 'http.response.body', 'body': b'{"error":"provider_admission_held"}'})
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.fence.settle(key)
