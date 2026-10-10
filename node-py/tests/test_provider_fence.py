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
