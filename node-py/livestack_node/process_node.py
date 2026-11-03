"""CPU-only Harmony facade with a disposable, privately listening GPU worker.

The public HTTP/WebSocket listener survives model eviction. A worker starts only
from Harmony's warm operation; public inference first asks the broker to grant
that warm. Evicting the last model terminates and reaps the worker's entire
delegated cgroup, including engines which create their own process sessions.
No accelerator library is imported by this process.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse


HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}


class ProcessNode:
    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.rows = {row['kind']: copy.deepcopy(row) for row in config['units']}
        if not self.rows or config['default_unit'] not in self.rows:
            raise ValueError('process_node_declared_default_required')
        self.process = None
        self.resident = set()
        self.active = 0
        self.last_used = time.monotonic()
        self.lock = asyncio.Lock()
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5), trust_env=False)
        self.worker_url = f"http://127.0.0.1:{config['worker_port']}"
        self.leases = {}
        self.control_headers = {}
        self.cgroup = None
        self.last_error = None
        self.last_child_report = {}
        self._memory = (0, None)
        self._closed = False
        self.sessions = {}

    def session(self, ident, resume=False):
        now=time.monotonic()
        self.sessions={k:v for k,v in self.sessions.items() if now-v['used']<300}
        if ident not in self.sessions or not resume:
            if len(self.sessions)>=128:raise RuntimeError('ASR session capacity exceeded')
            self.sessions[ident]={'frames':{},'bytes':0,'used':now,'final':None,'done':None,'start':None,'worker_pid':None}
        self.sessions[ident]['used']=now
        return self.sessions[ident]

    def cache_frame(self, session, frame):
        if frame[:4]!=b'BASR' or len(frame)<16:return
        seq=int.from_bytes(frame[8:16],'big')
        existing=session['frames'].get(seq)
        if existing is not None:
            if existing!=frame:raise RuntimeError('ASR sequence payload changed')
            return
        if (session['bytes']+len(frame)>64*1024*1024 or len(session['frames'])>=65536 or
                sum(s['bytes'] for s in self.sessions.values())+len(frame)>256*1024*1024):
            raise RuntimeError('ASR resume audio capacity exceeded')
        session['frames'][seq]=frame;session['bytes']+=len(frame);session['used']=time.monotonic()

    def _alive(self):
        if self.process is not None and self.process.returncode is not None:
            self.resident.clear()
            self.last_child_report = {}
            return False
        return self.process is not None

    async def _request(self, method, path, **kwargs):
        response = await self.http.request(method, self.worker_url + path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def _start(self):
        if self._alive():
            return
        if self.process is not None:
            await self._stop()
        env = {**os.environ, **self.config.get('env', {}),
               'LIVESTACK_REGISTER': '0', 'LIVESTACK_MESH_ENABLED': '0',
               'POLYASR_WARM_ON_START': '0', 'POLYTTS_WARM_ON_START': '0',
               'HARMONY_LLM_WARM_ON_START': '0', 'LIVESTACK_LISTEN_HOST':'127.0.0.1'}
        preexec = None
        if self.config.get('require_cgroup'):
            if sys.platform != 'linux':
                raise RuntimeError('process_node_linux_cgroup_required')
            relative = next(row[3:] for row in Path('/proc/self/cgroup').read_text().splitlines()
                            if row.startswith('0::'))
            parent = Path('/sys/fs/cgroup') / relative.lstrip('/')
            group = parent / 'harmony-gpu-worker'
            group.mkdir(exist_ok=True)
            if not (group / 'cgroup.kill').exists():
                raise RuntimeError('process_node_cgroup_kill_required')
            self.cgroup = group
            procs = str(group / 'cgroup.procs')
            def enter_cgroup():
                fd = os.open(procs, os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
            preexec = enter_cgroup
        self.process = await asyncio.create_subprocess_exec(
            *self.config['argv'], env=env, cwd=self.config.get('cwd'),
            start_new_session=True, preexec_fn=preexec)
        deadline = time.monotonic() + self.config.get('start_timeout', 90)
        while time.monotonic() < deadline:
            if not self._alive():
                raise RuntimeError('process_node_worker_exited_during_start')
            try:
                await self._request('GET', '/livestack/residence', timeout=2)
                if self.config.get('verify_loopback',True):
                    await self._verify_listener()
                return
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(.1)
        await self._stop()
        raise RuntimeError('process_node_worker_start_deadline')

    async def _verify_listener(self):
        port = self.config['worker_port']
        if sys.platform=='linux':
            found=[]
            for name in ['tcp','tcp6']:
                for row in Path('/proc/net/'+name).read_text().splitlines()[1:]:
                    fields=row.split();address,number=fields[1].split(':')
                    if int(number,16)==port and fields[3]=='0A':found.append(address)
            if not found or any(a not in ['0100007F','00000000000000000000000001000000'] for a in found):
                raise RuntimeError('process_node_worker_must_listen_on_loopback')
        elif sys.platform=='darwin':
            p=await asyncio.create_subprocess_exec('/usr/sbin/lsof','-nP',f'-iTCP:{port}',
                '-sTCP:LISTEN','-Fn',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            raw,_=await p.communicate();names=[r[1:] for r in raw.decode().splitlines() if r.startswith('n')]
            if not names or any(not n.startswith(('127.0.0.1:','[::1]:')) for n in names):
                raise RuntimeError('process_node_worker_must_listen_on_loopback')

    async def _stop(self):
        child = self.process
        if child is None:
            return
        deadline = time.monotonic() + self.config.get('stop_timeout', 15)
        if self.cgroup is not None:
            # The delegated group is created by this facade, never a caller path.
            for pid in (self.cgroup / 'cgroup.procs').read_text().split():
                try: os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError: pass
            while time.monotonic() < deadline:
                if 'populated 0' in (self.cgroup / 'cgroup.events').read_text(): break
                await asyncio.sleep(.05)
            if 'populated 1' in (self.cgroup / 'cgroup.events').read_text():
                (self.cgroup / 'cgroup.kill').write_text('1')
            for _ in range(100):
                if 'populated 0' in (self.cgroup / 'cgroup.events').read_text(): break
                await asyncio.sleep(.05)
            else:
                raise RuntimeError('process_node_descendants_not_settled')
        else:
            try: os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError: pass
        try:
            await asyncio.wait_for(child.wait(), max(.1, deadline-time.monotonic()))
        except asyncio.TimeoutError:
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            await asyncio.wait_for(child.wait(), 5)
        # The kernel has now destroyed the worker's accelerator contexts.
        self.process = None
        self.resident.clear()
        self.last_child_report = {}
        self.last_error = None
        if self.cgroup is not None:
            self.cgroup.rmdir()
            self.cgroup = None

    async def _refresh(self):
        if not self._alive():
            return
        try:
            report = await self._request('GET', '/livestack/residence', timeout=2)
            self.last_child_report = report
            self.resident = {r['kind'] for r in report.get('units', []) if r.get('resident')}
            self.last_error = None
        except (httpx.HTTPError, ValueError):
            # A slow/unknown worker isn't an empty device. Keep the last known
            # residents busy until the kernel proves that this process died.
            self.last_error = 'worker_state_unknown'

    def _protected(self):
        now = time.monotonic()
        self.leases = {k:v for k,v in self.leases.items() if v['deadline'] > now}
        return self.active or bool(self.leases) or any(
            r.get('busy') for r in self.last_child_report.get('units', []))

    async def warm(self, unit, headers=None, budget=None):
        if self.config.get('admission_hold'):raise HTTPException(503,self.config['admission_hold'])
        if unit not in self.rows:
            raise HTTPException(400, 'unknown managed unit')
        async with self.lock:
            await self._refresh()
            if unit in self.resident:
                self.last_used = time.monotonic()
                return {'resident':sorted(self.resident),'device':self.config['device_id']}
            if self._protected():
                raise HTTPException(409, 'worker is busy; retry admission')
            if headers: self.control_headers = headers
            try:
                await self._start()
                result = await self._request('POST', '/livestack/model/warm',
                    json={'unit':unit,'device':self.config['device_id'], **({'budget':budget} if budget else {})},
                    headers=self.control_headers, timeout=self.config.get('warm_timeout',600))
                if unit not in result.get('resident', []):
                    raise RuntimeError('worker_did_not_acknowledge_residency')
                self.resident = set(result['resident'])
                self.last_used = time.monotonic()
                return result
            except BaseException:
                if not self._protected(): await self._stop()
                raise

    async def evict(self, unit, headers=None):
        async with self.lock:
            await self._refresh()
            if self._protected(): raise HTTPException(409, 'active work or lease protects worker')
            if self._alive() and unit in self.resident:
                result = await self._request('POST', '/livestack/model/evict',
                    json={'unit':unit},headers=headers or self.control_headers,timeout=60)
                self.resident = set(result['resident'])
                if unit in self.resident: raise HTTPException(409, 'worker refused eviction')
            if not self.resident: await self._stop()
            return {'resident':sorted(self.resident),'worker_running':self._alive()}

    async def sweep(self):
        async with self.lock:
            await self._refresh()
            if (self._alive() and not self._protected() and
                    time.monotonic()-self.last_used >= self.config.get('idle_seconds',60)):
                await self._stop()

    async def admit(self, unit):
        broker = self.config.get('broker_url', 'http://127.0.0.1:8799')
        headers = {}
        if self.config.get('broker_token_file'):
            headers['Authorization'] = 'Bearer ' + Path(self.config['broker_token_file']).read_text().strip()
        response = await self.http.post(broker.rstrip('/')+'/admit',
            json={'kind':unit,'owner':'inference:'+self.config['node_id'],
                  'id':uuid.uuid4().hex},headers=headers,timeout=self.config.get('warm_timeout',600))
        response.raise_for_status()
        return response.json()

    async def begin(self, unit):
        if self.config.get('admission_hold'):raise HTTPException(503,self.config['admission_hold'])
        # Crucially outside self.lock: the broker dispatches /model/warm back
        # to this listener before acknowledging the grant.
        try: decision = await self.admit(unit)
        except Exception: raise HTTPException(503,'Harmony admission unavailable') from None
        if (decision.get('granted') is not True or decision.get('degraded') or
                decision.get('device_id') != self.config['device_id']):
            raise HTTPException(503,'Harmony did not grant this device')
        async with self.lock:
            if not self._alive() or unit not in self.resident:
                raise HTTPException(503,'Harmony grant did not warm this provider')
            self.active += 1
            self.last_used = time.monotonic()

    async def finish(self):
        async with self.lock:
            self.active -= 1
            self.last_used = time.monotonic()

    def voices(self):
        directory = self.config.get('voices_dir')
        if not directory: return {}
        result = {}
        for p in list(Path(directory).glob('*/meta.json'))[:4096]:
            try:
                if p.stat().st_size <= 65536:
                    result[p.parent.name] = json.loads(p.read_text())
            except (OSError,ValueError): pass
        return result

    async def choose(self, path, body, content_type=''):
        for prefix, unit in self.config.get('routes', {}).items():
            if path.startswith(prefix): return unit
        if self.config['kind']=='polytts' and 'json' in content_type:
            try:
                data = json.loads(body)
                engine = data.get('engine')
                if not engine and data.get('voice_id'):
                    engine = self.voices().get(data['voice_id'],{}).get('engine','qwen')
                if engine:
                    if engine not in self.rows: raise HTTPException(400,'unknown speech engine')
                    return engine
            except (ValueError,AttributeError): raise HTTPException(400,'invalid inference JSON') from None
        return self.config['default_unit']

    async def memory(self):
        if time.monotonic()-self._memory[0]<2: return self._memory[1]
        value = None
        if self.config.get('gpu_uuid'):
            try:
                p = await asyncio.create_subprocess_exec('nvidia-smi','-i',self.config['gpu_uuid'],
                    '--query-gpu=memory.total,memory.free','--format=csv,noheader,nounits',
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
                out,_ = await asyncio.wait_for(p.communicate(),3)
                total,free = map(int,out.decode().strip().split(','))
                value = {'capacity':{'vram_bytes':total*1048576},'free':{'vram_bytes':free*1048576}}
            except Exception: pass
        if value is None and self.last_child_report.get('device_mem'):
            value = self.last_child_report['device_mem']
        self._memory = (time.monotonic(),value)
        return value

    async def residence(self):
        await self._refresh()
        rows=[]
        for name, original in self.rows.items():
            row = copy.deepcopy(original)
            row.update(residency=2,resident=name in self.resident,
                       busy=bool(self._protected() or self.last_error))
            rows.append(row)
        result = dict(host_id=self.config['host_id'],node_id=self.config['node_id'],
                      device_id=self.config['device_id'],device_candidates=[self.config['device_id']],
                      units=rows,process_mem=self.last_child_report.get('process_mem') or
                      {'allocated_bytes':0,'reserved_bytes':0},
                      worker={'running':self._alive(),'pid':self.process.pid if self._alive() else None,
                              'last_error':self.last_error,'reclaim':'process-exit'})
        memory = await self.memory()
        if memory: result['device_mem']=memory
        return result

    async def capability(self):
        report=await self.residence()
        return dict(kind=self.config['kind'],host_id=self.config['host_id'],node_id=self.config['node_id'],
                    device_id=self.config['device_id'],device_candidates=[self.config['device_id']],
                    region=self.config.get('region'),units=list(self.rows),resident=sorted(self.resident),
                    identity={'state':'present'},ready=not bool(self.config.get('admission_hold')),
                    detail=self.config.get('admission_hold') or 'Harmony process-managed provider',
                    inventory={'voice':sorted(self.voices())} if self.config['kind']=='polytts' else self.config.get('inventory',{}),
                    load={'in_flight':self.active,'in_flight_source':'server',
                          'resident_units':len(self.resident)},worker=report['worker'])

    async def close(self):
        if self._closed:return
        self._closed=True
        async with self.lock: await self._stop()
        await self.http.aclose()


def create_app(node):
    async def maintain():
        while True:
            await asyncio.sleep(2)
            try: await node.sweep()
            except Exception as error: node.last_error=type(error).__name__

    async def announce():
        config=node.config
        body={'facade_url':config.get('announce_url',f"http://127.0.0.1:{config['port']}/livestack"),
              'host_id':config['host_id'],'kinds':[config['kind']]}
        if config.get('region'):body['region']=config['region']
        targets=[body['facade_url']]
        if config.get('mesh_url'):targets.append(config['mesh_url'])
        while True:
            for broker in config.get('announce_brokers',[config.get('broker_url','http://127.0.0.1:8799')]):
                for target in targets:
                    try: await node.http.post(broker.rstrip('/')+'/peers',json={**body,'facade_url':target},timeout=5)
                    except httpx.HTTPError: pass
            await asyncio.sleep(15)

    @asynccontextmanager
    async def lifespan(app):
        mesh=None
        if node.config.get('framework_root'):
            sys.path.insert(0,node.config['framework_root'])
            from livestack_node import mesh_attach,relay_control
            target=mesh_attach.resolve_mesh_target()
            if target:
                node.config['mesh_url']=target.advertised_url_for('/livestack')
                mesh=mesh_attach.start_mesh_attach(
                    f"http://127.0.0.1:{node.config['port']}",
                    relay_config=relay_control.RelayConfig.from_env(),daemon_id=target.daemon_id,
                    state=mesh_attach.MeshAttachState())
        tasks=[asyncio.create_task(maintain()),asyncio.create_task(announce())]
        try: yield
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            await node.close()
            if mesh:mesh.stop()

    app=FastAPI(lifespan=lifespan)

    @app.get('/livestack/residence')
    async def residence():return await node.residence()

    @app.get('/livestack/capability')
    async def capability():return await node.capability()

    @app.get('/health')
    async def health():
        c=await node.capability()
        return {'status':'ok' if not node.last_error else 'degraded','managed':True,
                'manager':{'resident':c['resident'],'idle_seconds':node.config.get('idle_seconds',60)},
                'worker':c['worker']}

    @app.get('/voices')
    async def voices():return {'voices':sorted(node.voices())}

    @app.get('/v1/models')
    async def models():
        return {'object':'list','data':[{'id':r.get('attributes',{}).get('model',n),'object':'model'}
                                      for n,r in node.rows.items()]}

    def control(request):
        # Preserve a deployment's existing authenticated node control contract.
        token_file=node.config.get('control_token_file')
        if token_file:
            import hmac
            expected='Bearer '+Path(token_file).read_text().strip()
            if not hmac.compare_digest(request.headers.get('authorization',''),expected):
                raise HTTPException(401,'Harmony node control credential required')
        elif os.environ.get('LIVESTACK_NODE_TOKENS_FILE') or os.environ.get('LIVESTACK_NODE_TOKENS'):
            from livestack_node.fleet_auth import principals_from_env,bearer_token,principal_for
            principals=principals_from_env(file_var='LIVESTACK_NODE_TOKENS_FILE',inline_var='LIVESTACK_NODE_TOKENS')
            try:principal_for(principals,bearer_token(request.headers.get('authorization')))
            except Exception:raise HTTPException(401,'Harmony node control credential required') from None
        return {'Authorization':request.headers['authorization']} if request.headers.get('authorization') else {}

    @app.post('/livestack/model/warm')
    async def warm(request:Request):
        headers=control(request);data=await request.json()
        if data.get('device',data.get('device_id',node.config['device_id'])) != node.config['device_id']:
            raise HTTPException(409,'wrong provider device')
        return await node.warm(data.get('unit'),headers,data.get('budget'))

    @app.post('/livestack/model/evict')
    async def evict(request:Request):
        headers=control(request);data=await request.json()
        return await node.evict(data.get('unit'),headers)

    @app.post('/livestack/model/reclaim')
    async def reclaim(request:Request):
        control(request)
        async with node.lock:
            await node._refresh()
            if not node._protected() and not node.resident:await node._stop()
        return {'reclaimed':not node._alive()}

    @app.post('/livestack/lease')
    async def lease(request:Request):
        control(request);data=await request.json();unit=data.get('kind',node.config['default_unit'])
        await node.begin(unit)
        try:
            ident=uuid.uuid4().hex;ttl=max(1,min(float(data.get('ttl_seconds') or 60),300))
            node.leases[ident]={'unit':unit,'deadline':time.monotonic()+ttl}
            return {'lease_id':ident,'kind':unit,'expires_at':time.monotonic()+ttl}
        finally:await node.finish()

    @app.post('/livestack/lease/{ident}/{operation}')
    async def lease_update(ident:str,operation:str,request:Request):
        control(request)
        if operation=='release':return {'released':node.leases.pop(ident,None) is not None}
        if operation!='heartbeat' or ident not in node.leases:raise HTTPException(404,'lease not found')
        data=await request.json();ttl=max(1,min(float(data.get('ttl_seconds') or 60),300))
        node.leases[ident]['deadline']=time.monotonic()+ttl
        return {'lease_id':ident,'expires_at':node.leases[ident]['deadline']}

    @app.api_route('/{path:path}',methods=['GET','POST','PUT','DELETE','PATCH'])
    async def proxy(path:str,request:Request):
        if path.startswith('livestack/'):raise HTTPException(404,'unknown Harmony control route')
        maximum=node.config.get('max_request_bytes',128*1024*1024)
        body=bytearray()
        async for chunk in request.stream():
            if len(body)+len(chunk)>maximum:raise HTTPException(413,'inference body exceeds limit')
            body.extend(chunk)
        unit=await node.choose('/'+path,body,request.headers.get('content-type',''))
        await node.begin(unit)
        try:
            headers={k:v for k,v in request.headers.items() if k.lower() not in HOP_HEADERS}
            req=node.http.build_request(request.method,node.worker_url+'/'+path,
                params=request.query_params,headers=headers,content=bytes(body))
            response=await node.http.send(req,stream=True)
        except BaseException:
            await node.finish();raise
        async def stream():
            try:
                async for data in response.aiter_raw():yield data
            finally:
                await response.aclose()
                await node.finish()
        return StreamingResponse(stream(),status_code=response.status_code,
            headers={k:v for k,v in response.headers.items() if k.lower() not in HOP_HEADERS})

    @app.websocket('/{path:path}')
    async def websocket(path:str,ws:WebSocket):
        import websockets
        await ws.accept()
        start=None
        first=None
        session=None
        try:
            # Session setup is CPU work. An app opening its dictation socket at
            # login must not allocate an accelerator until audio actually arrives.
            while first is None:
                message=await ws.receive()
                if message['type']=='websocket.disconnect':return
                if message.get('text'):
                    try:data=json.loads(message['text'])
                    except ValueError:data={}
                    if data.get('type') in ('start','resume'):
                        if data.get('protocol')!=1 or not data.get('sessionId'):
                            await ws.send_json({'type':'error','error':'invalid ASR session setup'});continue
                        start=message['text']
                        session=node.session(data['sessionId'],data['type']=='resume')
                        if data['type']=='start':session['start']=dict(data)
                        elif session['start'] is None:
                            session['start']={**data,'type':'start'}
                        elif 'context' in data and data['context']!=session['start'].get('context'):
                            await ws.send_json({'type':'error','error':'resume context does not match the original session'});return
                        ack={'type':'resumed' if data['type']=='resume' else 'started',
                             'protocol':1,'sessionId':data['sessionId'],'ackSeq':-1}
                        if int(data.get('control') or 1)>=2:ack['control']=2
                        await ws.send_json(ack)
                        continue
                first=message
            await node.begin(node.config['default_unit'])
            try:
                backend_url=node.worker_url.replace('http://','ws://')+'/'+path
                if ws.url.query:backend_url+='?'+ws.url.query
                async with websockets.connect(backend_url,max_size=node.config.get('max_request_bytes',128*1024*1024)) as backend:
                    if start:
                        if session is not None and session['worker_pid']!=node.process.pid:
                            start=json.dumps(session['start'])
                        await backend.send(start)
                        setup=await backend.recv()
                        try:setup_data=json.loads(setup)
                        except (ValueError,TypeError):setup_data={}
                        if setup_data.get('type') not in ('started','resumed'):
                            await ws.send_text(setup);return
                        if session is not None:session['worker_pid']=node.process.pid
                    async def send(message):
                        if message.get('bytes') is not None:
                            if session is not None:node.cache_frame(session,message['bytes'])
                            await backend.send(message['bytes'])
                        elif message.get('text') is not None:await backend.send(message['text'])
                    async def upstream():
                        while True:
                            message=await ws.receive()
                            if message['type']=='websocket.disconnect':return
                            await send(message)
                    async def downstream():
                        async for message in backend:
                            if isinstance(message,bytes):await ws.send_bytes(message)
                            else:
                                if session is not None:
                                    try:control_message=json.loads(message)
                                    except ValueError:control_message={}
                                    if control_message.get('type')=='final':
                                        session['final']=control_message
                                    elif control_message.get('type')=='done':session['done']=control_message
                                    session['used']=time.monotonic()
                                await ws.send_text(message)
                    tasks=[asyncio.create_task(downstream())]
                    try:
                        if session is not None:
                            for seq,frame in sorted(tuple(session['frames'].items())):await backend.send(frame)
                        await send(first)
                        tasks.append(asyncio.create_task(upstream()))
                        done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                        for task in done:task.result()
                    finally:
                        for task in tasks:task.cancel()
                        await asyncio.gather(*tasks,return_exceptions=True)
            finally:await node.finish()
        except WebSocketDisconnect:pass
        except RuntimeError:
            try:await ws.close(code=1013,reason="Harmony speech session unavailable; retry")
            except RuntimeError:pass
        except Exception:
            try:await ws.close(code=1013,reason='Harmony inference unavailable; retry')
            except RuntimeError:pass

    return app


def main():
    import uvicorn
    config_path=Path(sys.argv[1])
    if config_path.stat().st_size>1024*1024:raise ValueError('process_node_configuration_bound')
    config=json.loads(config_path.read_text())
    # A serving facade must never inherit a CUDA package path from its worker.
    if 'torch' in sys.modules or 'mlx.core' in sys.modules:
        raise RuntimeError('process_node_cpu_interpreter_required')
    if config.get('listen_ports'):
        import socket
        sockets=[]
        for port in config['listen_ports']:
            listener=socket.socket()
            listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            listener.bind((config.get('listen_host','0.0.0.0'),port))
            listener.listen(256);sockets.append(listener)
        uvicorn.Server(uvicorn.Config(create_app(ProcessNode(config)),log_level='warning')).run(sockets=sockets)
    else:
        uvicorn.run(create_app(ProcessNode(config)),host=config.get('listen_host','0.0.0.0'),port=config['port'],log_level='warning')


if __name__=='__main__':main()
