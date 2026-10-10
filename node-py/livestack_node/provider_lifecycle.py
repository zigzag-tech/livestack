"""Carry a current local owner's limited admission through actual ASGI lifecycle."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import copy_context
from threading import Event
from .provider_fence import FenceRefused


class ProviderLifecycle:
    def __init__(self, app, owner, fence, holder, server, synchronize, unload):
        self.app, self.owner, self.fence, self.holder = app, owner, fence, holder
        self.server, self.synchronize, self.unload = server, synchronize, unload
        self._shutdown_context = None
        self._shutdown_done = Event()
        self._shutdown_error = None
        self._started = False
        original = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application):
            context = original(application)
            receipt = fence.status(owner._credential)
            await fence.startup_owned(owner._credential, holder, receipt['epoch'], context.__aenter__, synchronize)
            self._started = True
            try:
                yield
            finally:
                if self._shutdown_context is None:
                    # Ordinary signal/socket shutdown is not authenticated drain.
                    raise FenceRefused('provider_shutdown_requires_owned_control')
                async def finish():
                    await self.unload()
                    await context.__aexit__(None, None, None)
                    synchronize()
                try:
                    task = self._shutdown_context.run(asyncio.create_task, finish())
                    await task
                except BaseException as exc:
                    self._shutdown_error = exc
                    raise
                finally:
                    self._shutdown_done.set()
        app.router.lifespan_context = lifespan

    def request_shutdown(self):
        # Invoked only inside shutdown_owned's current authenticated active grant.
        if not self._started or self._shutdown_context is not None:
            raise FenceRefused('provider_shutdown_lifecycle_not_available')
        self._shutdown_context = copy_context()
        self.server.should_exit = True
        self._shutdown_done.wait()
        if self._shutdown_error is not None:
            raise self._shutdown_error
