"""Actual ASGI path controls; no model imports or service effects."""
import asyncio
import unittest
from livestack_node.qualification_ingress import QualificationIngress

class QualificationIngressTests(unittest.TestCase):
    def invoke(self,provider,path,method='POST',kind='http'):
        called=[];events=[]
        async def app(scope,receive,send):called.append(scope['path'])
        async def receive():return {'type':'http.request','body':b''}
        async def send(event):events.append(event)
        asyncio.run(QualificationIngress(app,provider=provider)({'type':kind,'path':path,'method':method},receive,send))
        return called,events
    def test_legacy_inference_model_control_voice_and_ws_never_reach_app(self):
        for provider in ('polytts','polyasr'):
            for path in ('/tts','/transcribe','/v1/transcribe','/model/warm','/model/reclaim','/lease','/voices','/residence','/_owner/fence/release'):
                called,events=self.invoke(provider,path);self.assertEqual(called,[]);self.assertEqual(events[0]['status'],403)
            called,events=self.invoke(provider,'/ws',kind='websocket');self.assertEqual(called,[]);self.assertEqual(events[0]['code'],1008)
    def test_owned_submit_and_settlement_controls_keep_actual_endpoint_auth(self):
        for provider,prefix,result in (('polytts','/tts','audio'),('polyasr','/v1/align','result')):
            for path,method in ((prefix+'/capabilities','GET'),(prefix+'/jobs','POST'),(prefix+'/jobs/owned-id','GET'),(prefix+'/jobs/owned-id/'+result,'GET'),(prefix+'/jobs/owned-id/cancel','POST')):
                called,events=self.invoke(provider,path,method);self.assertEqual(called,[path]);self.assertEqual(events,[])
    def test_health_is_non_model_read_and_lifespan_is_preserved(self):
        called,events=self.invoke('polytts','/health','GET');self.assertEqual(called,[]);self.assertEqual(events[0]['status'],200)
        called,events=self.invoke('polyasr','/startup',kind='lifespan');self.assertEqual(called,['/startup']);self.assertEqual(events,[])

if __name__=='__main__':unittest.main()
