import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from livestack_node.provider_fence import ProviderFence, ProviderAdmissionMiddleware, FenceRefused


def authorize(token, action):
    if token != 'operator':
        raise PermissionError(action)


class ProviderFenceTests(unittest.TestCase):
    def test_timeout_does_not_drain_worker(self):
        fence = ProviderFence(authorize)
        started, finish = Event(), Event()
        def worker():
            started.set()
            finish.wait(2)
        with ThreadPoolExecutor(1) as base:
            future = fence.executor(base, lambda: None).submit(worker)
            self.assertTrue(started.wait(1))
            async def timeout():
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.wrap_future(future), .01)
            asyncio.run(timeout())
            receipt = fence.hold('operator', 'deploy')
            self.assertFalse(receipt['drained'])
            with self.assertRaises(FenceRefused):
                fence.executor(base, lambda: None).submit(worker)
            finish.set()
        self.assertTrue(fence.status('operator')['drained'])

    def test_failed_barrier_stays_uncertain_and_wrong_owner_refuses(self):
        fence = ProviderFence(authorize)
        def broken():
            raise RuntimeError('barrier')
        with ThreadPoolExecutor(1) as base:
            fence.executor(base, broken).submit(lambda: None).result()
        with self.assertRaises(PermissionError):
            fence.hold('caller', 'deploy')
        receipt = fence.hold('operator', 'deploy')
        self.assertEqual(receipt['uncertain'], 1)
        self.assertFalse(receipt['drained'])
        with self.assertRaises(FenceRefused):
            fence.release('operator', 'other', receipt['epoch'])
        fence.release('operator', 'deploy', receipt['epoch'])
        self.assertFalse(fence.hold('operator', 'next')['drained'])

    def test_held_http_ws_refuse_and_bypass_preserves_endpoint_auth(self):
        fence = ProviderFence(authorize)
        calls, messages = [], []
        async def app(scope, receive, send):
            calls.append(scope['path'])
            await send({'type':'http.response.start','status':401})
        middleware = ProviderAdmissionMiddleware(app, fence,
            lambda scope: scope['type']=='http' and scope['path']=='/owned/status')
        async def send(message):
            messages.append(message)
        fence.hold('operator','deploy')
        asyncio.run(middleware({'type':'http','path':'/tts'},None,send))
        asyncio.run(middleware({'type':'websocket','path':'/ws/transcribe'},None,send))
        asyncio.run(middleware({'type':'http','path':'/owned/status'},None,send))
        self.assertEqual(calls,['/owned/status'])
        self.assertEqual(messages[0]['status'],503)
        self.assertEqual(messages[2]['code'],1013)
        self.assertEqual(messages[3]['status'],401)
        self.assertTrue(fence.status('operator')['drained'])


class OperatorApiTests(unittest.TestCase):
    def test_real_asgi_operator_auth_cas_and_source_receipt(self):
        import httpx
        from fastapi import FastAPI
        from livestack_node.provider_fence import install_provider_fence
        app = FastAPI()
        @app.post('/legacy')
        def legacy():
            return {'ok': True}
        install_provider_fence(app, ProviderFence(authorize), lambda scope: False,
                               {'sourceSha256':'reviewed-source-fixture'})
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                denied = await client.post('/_owner/fence/hold',json={'holder':'deploy'})
                self.assertEqual(denied.status_code,403)
                held = await client.post('/_owner/fence/hold',headers={'Authorization':'operator'},json={'holder':'deploy'})
                self.assertTrue(held.json()['drained'])
                self.assertEqual(held.json()['sourceIdentity']['sourceSha256'],'reviewed-source-fixture')
                self.assertEqual((await client.post('/legacy')).status_code,503)
                wrong = await client.post('/_owner/fence/release',headers={'Authorization':'operator'},json={'holder':'other','epoch':held.json()['epoch']})
                self.assertEqual(wrong.status_code,409)
                released = await client.post('/_owner/fence/release',headers={'Authorization':'operator'},json={'holder':'deploy','epoch':held.json()['epoch']})
                self.assertEqual(released.status_code,200)
                self.assertEqual((await client.post('/legacy')).status_code,200)
        asyncio.run(run())


class ShutdownTests(unittest.TestCase):
    def test_only_current_authenticated_drained_owner_shutdown_can_dispatch(self):
        fence = ProviderFence(authorize)
        receipt = fence.hold('operator','deploy')
        with self.assertRaises(PermissionError):
            fence.shutdown_owned('caller','deploy',receipt['epoch'],lambda:None,lambda:None)
        with self.assertRaises(FenceRefused):
            fence.shutdown_owned('operator','deploy',receipt['epoch']+1,lambda:None,lambda:None)
        with ThreadPoolExecutor(1) as base:
            executor = fence.executor(base,lambda:None)
            def shutdown():
                self.assertFalse(fence.status('operator')['drained'])
                return executor.submit(lambda:'released').result()
            self.assertEqual(fence.shutdown_owned('operator','deploy',receipt['epoch'],shutdown,lambda:None),'released')
            with self.assertRaises(FenceRefused):
                executor.submit(lambda:None)
        self.assertTrue(fence.status('operator')['drained'])
