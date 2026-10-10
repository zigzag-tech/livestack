import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from fastapi import FastAPI
from livestack_node.provider_fence import ProviderFence, FenceRefused
from livestack_node.provider_owner_socket import ProviderOwnerSocket
from livestack_node.provider_lifecycle import ProviderLifecycle


class LifecycleTests(unittest.TestCase):
    def test_actual_asgi_startup_and_owned_shutdown_keep_physical_fence(self):
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(1) as base:
            config=Path(directory)/'config';config.write_text('{}');config.chmod(0o600)
            app=FastAPI();events=[]
            owner=ProviderOwnerSocket(Path(directory)/'control',config,{'source':'fixture'},lambda:None,lambda:None)
            fence=ProviderFence(owner.authorize)
            executor=fence.executor(base,lambda:None)
            @app.on_event('startup')
            async def startup():
                await asyncio.get_running_loop().run_in_executor(executor,lambda:events.append('startup'))
            @app.on_event('shutdown')
            async def shutdown():
                await asyncio.get_running_loop().run_in_executor(executor,lambda:events.append('shutdown'))
            receipt=fence.hold(owner._credential,'activation')
            server=SimpleNamespace(should_exit=False)
            async def unload():
                self.assertFalse(fence.status(owner._credential)['drained'])
                await asyncio.get_running_loop().run_in_executor(executor,lambda:events.append('unload'))
            lifecycle=ProviderLifecycle(app,owner,fence,'activation',server,lambda:None,unload)
            owner.shutdown=lifecycle.request_shutdown
            owner.start(fence)
            async def run():
                async with app.router.lifespan_context(app):
                    self.assertTrue(fence.status(owner._credential)['drained'])
                    with self.assertRaises(FenceRefused):executor.submit(lambda:None)
                    request=asyncio.create_task(asyncio.to_thread(owner.dispatch,owner.uid,
                        {'operation':'shutdown','holder':'activation','epoch':receipt['epoch'],'expectedSource':{'source':'fixture'}}))
                    while not server.should_exit:
                        if request.done():
                            await request
                        await asyncio.sleep(.001)
                result=await request
                self.assertTrue(result['drained'])
            try:
                asyncio.run(run())
            finally:
                owner.close()
            self.assertEqual(events,['startup','unload','shutdown'])
