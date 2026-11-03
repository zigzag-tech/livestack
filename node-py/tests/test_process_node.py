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
            with TestClient(create_app(self.node))as c:
                with c.websocket_connect('/ws/transcribe')as ws:
                    ws.send_json({'type':'start','protocol':1,'sessionId':'replay','context':'private context'})
                    self.assertEqual(ws.receive_json()['type'],'started')
                    ws.send_bytes(frame);self.assertEqual(ws.receive_json()['ackSeq'],0)
                self.assertEqual(c.post('/livestack/model/evict',json={'unit':'asr'}).status_code,200)
                with c.websocket_connect('/ws/transcribe')as ws:
                    ws.send_json({'type':'resume','protocol':1,'sessionId':'replay'})
                    self.assertEqual(ws.receive_json()['type'],'resumed')
                    ws.send_json({'type':'stop'})
                    self.assertEqual(ws.receive_json()['type'],'ack')
                    self.assertEqual(ws.receive_json(),{'type':'final','text':'private context','frames':1})
        await asyncio.to_thread(request)


if __name__ == '__main__': unittest.main()
