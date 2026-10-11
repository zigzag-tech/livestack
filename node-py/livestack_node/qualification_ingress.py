"""Cold qualification surface: no legacy inference, public warm, or WebSocket.

Install only on a separately configured qualification unit. Owned settlement
controls keep their existing token checks and remain reachable after grant expiry.
"""
import json
import re


class QualificationIngress:
    def __init__(self, app, *, provider):
        if provider not in ('polytts','polyasr'):
            raise ValueError('qualification_provider_required')
        self.app,self.provider = app,provider

    async def __call__(self,scope,receive,send):
        kind = scope['type']
        if kind not in ('http','websocket'):
            return await self.app(scope,receive,send)
        if kind=='websocket':
            await send({'type':'websocket.close','code':1008});return
        path,method = scope['path'],scope.get('method','')
        if path=='/health' and method=='GET':
            body=json.dumps({'status':'qualification-only','inference_admission':'restricted'}).encode()
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':body});return
        prefix = '/tts' if self.provider=='polytts' else '/v1/align'
        result = '/audio' if self.provider=='polytts' else '/result'
        allowed = method=='GET' and path==prefix+'/capabilities' or method=='POST' and path==prefix+'/jobs'
        allowed = allowed or bool(method=='GET' and re.fullmatch(re.escape(prefix)+r'/jobs/[A-Za-z0-9-]+(?:'+re.escape(result)+r')?',path))
        allowed = allowed or bool(method=='POST' and re.fullmatch(re.escape(prefix)+r'/jobs/[A-Za-z0-9-]+/cancel',path))
        if not allowed:
            body=b'{"detail":"qualification-only ingress refuses this operation"}'
            await send({'type':'http.response.start','status':403,'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':body});return
        return await self.app(scope,receive,send)
