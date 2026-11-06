"""Private real-process fixture: no GPU, production service or model weights."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient

from livestack_node.process_node import ProcessNode, create_app


WORKER = r'''
import json,sys,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
resident=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def do_GET(self):
  body={'resident':resident,'units':[{'kind':'asr','resident':'asr' in resident}], 'pid':__import__('os').getpid()}
  self.send_response(200);self.end_headers();self.wfile.write(json.dumps(body).encode())
 def do_POST(self):
  data=self.rfile.read(int(self.headers.get('Content-Length','0')))
  if self.path=='/trickle':
   self.send_response(200);self.end_headers()
   for _ in range(40):
    try:self.wfile.write(b'x');self.wfile.flush();time.sleep(.05)
    except OSError:return
   return
  if self.path.endswith('/warm'): resident.append(json.loads(data)['unit'])
  elif self.path.endswith('/evict'): resident.clear()
  elif self.path=='/slow': time.sleep(.3)
  self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'resident':resident,'bytes':len(data)}).encode())
ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
'''


class ProcessNodeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        script = Path(self.temp.name) / 'worker.py'
        script.write_text(WORKER)
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        self.cfg = dict(host_id='private-host', node_id='private-node', kind='polyasr',
                        device_id='private-host/gpu0', port=0, worker_port=port,
                        argv=[sys.executable, str(script), str(port)], env={},
                        units=[{'kind':'asr','footprint':{'vram_bytes':100},'attributes':{}}],
                        default_unit='asr', idle_seconds=1, start_timeout=5, stop_timeout=2)
        self.node = ProcessNode(self.cfg)
        self.addAsyncCleanup(self.node.close)

    async def test_operator_hold_prevents_warm_and_reports_not_ready(self):
        self.node.config['admission_hold']='broker retirement qualification required'
        self.assertFalse((await self.node.capability())['ready'])
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):await self.node.warm('asr')
        self.assertIsNone(self.node.process)

    async def test_boot_is_cpu_only_and_has_no_worker(self):
        self.assertIsNone(self.node.process)
        self.assertEqual((await self.node.capability())['resident'], [])
        self.assertNotIn('torch', sys.modules)
        self.assertNotIn('mlx.core', sys.modules)

    async def test_warm_then_evict_reaps_the_gpu_process(self):
        await self.node.warm('asr')
        child = self.node.process
        self.assertIsNotNone(child)
        self.assertIn('asr', (await self.node.capability())['resident'])
        await self.node.evict('asr')
        self.assertIsNotNone(child.returncode)
        self.assertIsNone(self.node.process)
        self.assertEqual((await self.node.capability())['resident'], [])

    async def test_denied_or_degraded_admission_never_starts_worker(self):
        for decision in [{'granted':False}, {'granted':True,'degraded':'broker fault'}]:
            async def admit(_unit, answer=decision): return answer
            self.node.admit = admit
            app = create_app(self.node)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://private') as c:
                response = await c.post('/v1/audio/transcriptions',content=b'audio')
                self.assertEqual(response.status_code, 503)
            self.assertIsNone(self.node.process)

    async def test_a_grant_for_a_unit_the_broker_thinks_resident_warms_it_instead_of_refusing(self):
        # Observed 2026-11-06: the facade reclaimed an idle worker; the broker still
        # remembered the unit resident, granted without dispatching a warm, and the
        # session was refused "grant did not warm this provider".
        async def admit(unit): return {'granted':True,'device_id':self.cfg['device_id']}   # no warm dispatched
        self.node.admit = admit
        self.assertIsNone(self.node.process)
        token = await self.node.begin('asr')
        self.assertIsNotNone(self.node.process)
        self.assertIn('asr', self.node.resident)
        self.assertIn(token, self.node.holds)
        self.node.finish(token)

    async def test_the_residence_report_says_its_owner_vouches_for_absence(self):
        report = await self.node.residence()
        self.assertEqual(report['residence_authority'], 'owner')
        self.assertTrue(all(not u['resident'] for u in report['units']))

    async def test_refusal_carries_the_planner_reason_and_busy_device_reports_not_ready(self):
        async def admit(_unit): return {'granted':False,'defer_reason':'device held by an image job'}
        self.node.admit = admit
        app = create_app(self.node)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://private') as c:
            response = await c.post('/v1/audio/transcriptions',content=b'audio')
            self.assertEqual(response.status_code, 503)
            self.assertIn('device held by an image job', response.json()['detail'])
            self.assertEqual(response.headers['retry-after'], '30')
            # Positive control: with no lease the same node is ready and healthy.
            self.assertEqual((await c.get('/health')).status_code, 200)
            lease = {'device_id':self.cfg['device_id'],'state':'exclusive','owner':'harmony-image'}
            class R:
                def raise_for_status(self): pass
                def json(self): return {'device_leases':[lease]}
            async def get(*_a, **_k): return R()
            self.node.http.get = get
            self.node._busy = (-10.0, None)
            cap = await self.node.capability()
            self.assertFalse(cap['ready'])
            self.assertIn('harmony-image', cap['detail'])
            self.assertIn('harmony-image', cap['device_busy'])
            health = await c.get('/health')
            self.assertEqual(health.status_code, 503)
            self.assertEqual(health.json()['status'], 'device_busy')

    async def test_two_callers_use_a_grant_and_busy_work_blocks_eviction(self):
        grants = []
        async def admit(unit):
            grants.append(unit)
            await self.node.warm(unit)
            return {'granted':True,'device_id':self.cfg['device_id']}
        self.node.admit = admit
        app = create_app(self.node)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://private') as a:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://private') as b:
                pending = asyncio.create_task(a.post('/slow',content=b'one'))
                for _ in range(100):
                    if self.node.active: break
                    await asyncio.sleep(.01)
                eviction = await b.post('/livestack/model/evict',json={'unit':'asr'})
                self.assertEqual(eviction.status_code,409)
                second = await b.post('/v1/audio/transcriptions',content=b'two')
                self.assertEqual(second.status_code,200)
                self.assertEqual((await pending).status_code,200)
        self.assertEqual(grants,['asr','asr'])
        self.assertEqual(self.node.active,0)

    async def test_a_hold_never_outlives_the_request_that_took_it(self):
        async def admit(unit):
            await self.node.warm(unit)
            return {'granted':True,'device_id':self.cfg['device_id']}
        self.node.admit=admit
        await self.node.begin('asr')
        self.assertEqual(self.node.active,1)
        self.assertTrue((await self.node.residence())['units'][0]['busy'])
        async def request_that_skips_its_cleanup():await self.node.begin('asr')
        await asyncio.create_task(request_that_skips_its_cleanup())
        self.assertEqual(self.node.active,1,'only the live request is in flight')
        self.node.holds.clear()
        self.assertFalse((await self.node.residence())['units'][0]['busy'])

    async def test_client_gone_mid_response_releases_its_hold_while_the_sweep_holds_the_lock(self):
        # xc-tower-ubuntu, 2026-11-03: a hold leaked this way reported asr
        # busy over an idle worker for hours and blocked exclusive admission.
        import uvicorn
        async def admit(unit):
            await self.node.warm(unit)
            return {'granted':True,'device_id':self.cfg['device_id']}
        self.node.admit=admit
        self.node.config['announce_brokers']=[]
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(create_app(self.node),host='127.0.0.1',port=port,
                                             log_level='error',lifespan='off'))
        serving=asyncio.create_task(server.serve())
        async def stop():
            server.should_exit=True
            await serving
        self.addAsyncCleanup(stop)
        while not server.started:await asyncio.sleep(.02)
        async def sweep_polling_the_worker():
            async with self.node.lock:await asyncio.sleep(.5)
        async with httpx.AsyncClient() as c:
            async with c.stream('POST',f'http://127.0.0.1:{port}/trickle',content=b'audio') as r:
                async for _ in r.aiter_raw():break
                self.assertEqual(len(self.node.holds),1)
                sweep=asyncio.create_task(sweep_polling_the_worker())
                await asyncio.sleep(0)
        await sweep
        await asyncio.sleep(.2)
        self.assertEqual(self.node.holds,{},'the hold is returned by its request, not pruned later')

    async def test_idle_eviction_terminates_worker(self):
        await self.node.warm('asr')
        child = self.node.process
        self.node.last_used -= 2
        await self.node.sweep()
        self.assertIsNone(self.node.process)
        self.assertIsNotNone(child.returncode)

    async def test_idle_process_exit_clears_transient_poll_failure(self):
        from unittest.mock import AsyncMock
        await self.node.warm('asr')
        self.node.last_used-=2
        self.node._request=AsyncMock(side_effect=httpx.ReadTimeout('private timeout'))
        await self.node.sweep()
        self.assertIsNone(self.node.process)
        self.assertIsNone(self.node.last_error)

    async def test_dead_child_cannot_leave_a_phantom_busy_model(self):
        await self.node.warm('asr')
        self.node.last_child_report={'units':[{'kind':'asr','resident':True,'busy':True}]}
        self.node.process.kill();await self.node.process.wait()
        await self.node.capability()
        self.assertFalse(self.node._protected())
        await self.node.warm('asr')
        self.assertIn('asr',self.node.resident)

    async def test_child_death_is_observed_instead_of_stale_residency(self):
        await self.node.warm('asr')
        self.node.process.kill()
        await self.node.process.wait()
        self.assertEqual((await self.node.capability())['resident'], [])

    async def test_websocket_start_ack_does_not_allocate_gpu(self):
        self.node.config['admit_on']='first_audio'
        # The real synchronous client runs off the asyncio fixture thread.
        def request():
            with TestClient(create_app(self.node)) as c:
                with c.websocket_connect('/ws/transcribe') as ws:
                    ws.send_json({'type':'start','protocol':1,'sessionId':'private-session'})
                    self.assertEqual(ws.receive_json(),{'type':'started','protocol':1,
                                                       'sessionId':'private-session','ackSeq':-1})
        await asyncio.to_thread(request)
        self.assertIsNone(self.node.process)

    async def test_resume_audio_lives_in_cpu_frontend_across_worker_eviction(self):
        await self.node.warm('asr')
        session=self.node.session('resume-session')
        frame=b'BASR'+bytes([1,1,0,0])+(0).to_bytes(8,'big')+b'audio'
        self.node.cache_frame(session,frame)
        await self.node.evict('asr')
        resumed=self.node.session('resume-session',True)
        self.assertEqual(resumed['frames'][0],frame)
        self.assertIsNone(self.node.process)

    async def test_resume_cache_is_bounded_and_sequence_rewrites_refused(self):
        s=self.node.session('session')
        frame=b'BASR'+bytes([1,1,0,0])+(3).to_bytes(8,'big')+b'audio'
        self.node.cache_frame(s,frame)
        with self.assertRaisesRegex(RuntimeError,'changed'):
            self.node.cache_frame(s,frame[:-1]+b'x')
        s['bytes']=64*1024*1024
        with self.assertRaisesRegex(RuntimeError,'capacity'):
            self.node.cache_frame(s,frame[:8]+(4).to_bytes(8,'big')+b'audio')

    async def test_real_websocket_replays_audio_and_context_after_worker_eviction(self):
        self.node.config['admit_on']='first_audio'
        script=Path(self.cfg['argv'][1])
        script.write_text("""
import json,sys
from fastapi import FastAPI,WebSocket
import uvicorn
app=FastAPI();resident=[]
@app.get('/livestack/residence')
def residence():return {'units':[{'kind':'asr','resident':'asr'in resident}]}
@app.post('/livestack/model/warm')
def warm():resident.append('asr');return {'resident':resident}
@app.post('/livestack/model/evict')
def evict():resident.clear();return {'resident':resident}
@app.websocket('/ws/transcribe')
async def ws(w:WebSocket):
 await w.accept();setup=await w.receive_json();await w.send_json({'type':'started'})
 frames={}
 try:
  while True:
   message=await w.receive()
   if message['type']=='websocket.disconnect':return
   if message.get('bytes'):
    frame=message['bytes'];frames[int.from_bytes(frame[8:16],'big')]=frame
    await w.send_json({'type':'ack','ackSeq':max(frames)})
   elif json.loads(message['text'])['type']=='stop':
    await w.send_json({'type':'final','text':setup.get('context'),'frames':len(frames)});return
 except Exception:pass
uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[1]),log_level='error')
""")
        async def admit(unit):
            await self.node.warm(unit)
            return {'granted':True,'device_id':self.cfg['device_id']}
        self.node.admit=admit
        frame=b'BASR'+bytes([1,1,0,0])+(0).to_bytes(8,'big')+b'audio'
        def request():
            def frames(ws):
                # `warming` progress frames are advisory and may precede any reply.
                while True:
                    m=ws.receive_json()
                    if m['type']!='warming':return m
            with TestClient(create_app(self.node))as c:
                with c.websocket_connect('/ws/transcribe')as ws:
                    ws.send_json({'type':'start','protocol':1,'sessionId':'replay','context':'private context'})
                    self.assertEqual(ws.receive_json()['type'],'started')
                    ws.send_bytes(frame);self.assertEqual(frames(ws)['ackSeq'],0)
                self.assertEqual(c.post('/livestack/model/evict',json={'unit':'asr'}).status_code,200)
                with c.websocket_connect('/ws/transcribe')as ws:
                    ws.send_json({'type':'resume','protocol':1,'sessionId':'replay'})
                    self.assertEqual(ws.receive_json()['type'],'resumed')
                    ws.send_json({'type':'stop'})
                    self.assertEqual(frames(ws)['type'],'ack')
                    self.assertEqual(frames(ws),{'type':'final','text':'private context','frames':1})
        await asyncio.to_thread(request)

    # --- warm admission: the load overlaps with the person starting to speak ---

    async def test_admission_starts_at_session_start_and_reports_warming(self):
        async def admit(unit):
            await asyncio.sleep(.6)
            await self.node.warm(unit)
            return {'granted':True,'device_id':self.cfg['device_id']}
        self.node.admit=admit
        self.node.config['warm_expected_s']=20
        seen=[]
        def request():
            with TestClient(create_app(self.node)) as c:
                with c.websocket_connect('/ws/transcribe') as ws:
                    ws.send_json({'type':'start','protocol':1,'sessionId':'warm-session'})
                    seen.append(ws.receive_json())
                    # No audio has been sent: admission already runs, and the
                    # client is told so instead of waiting in silence.
                    seen.append(ws.receive_json())
        await asyncio.to_thread(request)
        self.assertEqual(seen[0]['type'],'started')
        self.assertEqual(seen[1]['type'],'warming')
        self.assertEqual(seen[1]['expectedMs'],20000)
        self.assertEqual(seen[1]['sessionId'],'warm-session')
        self.assertGreaterEqual(seen[1]['elapsedMs'],200)

    async def test_refused_admission_says_why_in_frame_and_close(self):
        async def admit(_unit):return {'granted':False}
        self.node.admit=admit
        from starlette.websockets import WebSocketDisconnect
        got={}
        def request():
            with TestClient(create_app(self.node)) as c:
                with c.websocket_connect('/ws/transcribe') as ws:
                    ws.send_json({'type':'start','protocol':1,'sessionId':'refused'})
                    ws.receive_json()
                    got['frame']=ws.receive_json()
                    try:ws.receive_json()
                    except WebSocketDisconnect as exc:got['close']=(exc.code,exc.reason)
        await asyncio.to_thread(request)
        self.assertEqual(got['frame'],{'type':'error','error':'Harmony did not grant this device'})
        self.assertEqual(got['close'],(1013,'Harmony did not grant this device'))
        self.assertEqual(self.node.active,0)

    async def test_disconnect_before_audio_releases_the_grant(self):
        async def admit(unit):
            await self.node.warm(unit)
            return {'granted':True,'device_id':self.cfg['device_id']}
        self.node.admit=admit
        def request():
            with TestClient(create_app(self.node)) as c:
                with c.websocket_connect('/ws/transcribe') as ws:
                    ws.send_json({'type':'start','protocol':1,'sessionId':'abandoned'})
                    ws.receive_json()
                    import time as _t;_t.sleep(1.5)
        await asyncio.to_thread(request)
        await asyncio.sleep(.2)
        self.assertEqual(self.node.active,0,'a socket that never sent audio must not pin the device')

    # --- adaptive keep-warm ---------------------------------------------------

    async def test_default_idle_policy_is_unchanged(self):
        await self.node.warm('asr')
        self.node.last_served=__import__('time').monotonic()
        self.node.last_used-=2
        await self.node.sweep()
        self.assertIsNone(self.node.process,'keep_warm_after_use_s defaults to off')

    async def test_a_served_worker_stays_warm_longer_but_a_speculative_one_does_not(self):
        self.node.config.update(keep_warm_after_use_s=30,keep_warm_max_s=900)
        await self.node.warm('asr')            # speculative: nothing served
        self.node.last_used-=2
        await self.node.sweep()
        self.assertIsNone(self.node.process,'a never-used warm is released at the floor')
        await self.node.warm('asr')
        self.node.last_served=__import__('time').monotonic()   # a real session ended
        self.node.last_used-=2
        await self.node.sweep()
        self.assertIsNotNone(self.node.process,'a worker that just served stays warm')
        self.node.last_used-=40
        await self.node.sweep()
        self.assertIsNone(self.node.process,'and is still released after the window')

    async def test_keep_warm_is_capped_and_never_below_the_floor(self):
        self.node.config.update(keep_warm_after_use_s=10**6,keep_warm_max_s=120)
        self.node.last_served=1.0
        self.assertEqual(self.node.keep_warm_window(),120)
        self.node.config.update(keep_warm_after_use_s=5,idle_seconds=60)
        self.assertEqual(self.node.keep_warm_window(),60)

    async def test_planner_eviction_ignores_the_keep_warm_window(self):
        self.node.config.update(keep_warm_after_use_s=600)
        await self.node.warm('asr')
        self.node.last_served=__import__('time').monotonic()
        await self.node.evict('asr')
        self.assertIsNone(self.node.process,'keep-warm is not a pin')


if __name__ == '__main__': unittest.main()
