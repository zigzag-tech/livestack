import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from livestack_node.provider_owner_probe import control as probe_control
from livestack_node.provider_fence import FenceRefused, ProviderFence
from livestack_node.provider_owner_socket import ProviderOwnerSocket


class OwnerSocketTests(unittest.TestCase):
    def test_actual_private_socket_custody_source_cas_and_closed_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root/'owner.json';config.write_text('{}');config.chmod(0o600)
            effects = []
            control = ProviderOwnerSocket(root/'control.sock',config,{'source':'fixture'},lambda:effects.append('shutdown'),lambda:None)
            fence = ProviderFence(control.authorize)
            control.start(fence)
            try:
                self.assertEqual(os.stat(control.path).st_mode & 0o777,0o600)
                observed=probe_control(control.path,{'operation':'status'})
                self.assertEqual(observed['peer']['pid'],os.getpid())
                self.assertEqual(observed['receipt']['result']['serverProcessId'],os.getpid())
                self.assertGreater(observed['peer']['startTicks'],0)
                with self.assertRaisesRegex(PermissionError,'effect_peer'):
                    probe_control(control.path,{'operation':'hold','holder':'wrong-unit','expectedSource':{'source':'fixture'}},
                                  {'pid':os.getpid()+1,'startTicks':observed['peer']['startTicks']})
                self.assertIsNone(fence._status()['holder'])
                dispatch=control.dispatch
                def wrong_process(uid,request):
                    value=dispatch(uid,request);value['serverProcessId']=os.getpid()+1;return value
                with patch.object(control,'dispatch',wrong_process):
                    with self.assertRaisesRegex(PermissionError,'peer_process'):probe_control(control.path,{'operation':'status'})
                def call(request):
                    with socket.socket(socket.AF_UNIX) as client:
                        client.connect(control.path)
                        client.sendall(json.dumps(request).encode()+b'\n')
                        return json.loads(client.makefile('rb').readline())
                with self.assertRaises(PermissionError):
                    control.dispatch(os.getuid()+1,{'operation':'status'})
                with self.assertRaises(PermissionError):
                    fence.hold('Bearer public-profile','deploy')
                self.assertFalse(call({'operation':'hold','holder':'deploy','expectedSource':{'source':'wrong'}})['ok'])
                active = fence.admit()
                held = call({'operation':'hold','holder':'deploy','expectedSource':{'source':'fixture'}})['result']
                self.assertFalse(held['drained'])
                blocked = {'operation':'shutdown','holder':'deploy','epoch':held['epoch'],'expectedSource':{'source':'fixture'}}
                self.assertFalse(call(blocked)['ok']);self.assertEqual(effects,[])
                fence.settle(active)
                wrong = {'operation':'shutdown','holder':'deploy','epoch':held['epoch']+1,'expectedSource':{'source':'fixture'}}
                self.assertFalse(call(wrong)['ok']);self.assertEqual(effects,[])
                self.assertTrue(call({**wrong,'epoch':held['epoch']})['ok']);self.assertEqual(effects,['shutdown'])
                with self.assertRaises(FenceRefused):fence.admit()
            finally:control.close()
            config.chmod(0o644)
            with self.assertRaises(PermissionError):
                ProviderOwnerSocket(root/'bad.sock',config,{},lambda:None,lambda:None)

    def test_private_qualification_lane_rejects_public_uid_source_and_extra_field(self):
        import time
        with tempfile.TemporaryDirectory()as directory:
            root=Path(directory);config=root/'owner.json';config.write_text('{}');config.chmod(0o600)
            owner=ProviderOwnerSocket(root/'control.sock',config,{'source':'CPU fixture'},lambda:None,lambda:None)
            fence=ProviderFence(owner.authorize);owner.start(fence)
            try:
                held=owner.dispatch(os.getuid(),{'operation':'hold','holder':'qualification','expectedSource':owner.source_identity})
                request={'operation':'qualification-release','holder':'qualification','epoch':held['epoch'],'seconds':1,'expectedSource':owner.source_identity}
                with self.assertRaises(PermissionError):owner.dispatch(os.getuid()+1,request)
                with self.assertRaises(FenceRefused):owner.dispatch(os.getuid(),{**request,'expectedSource':{}})
                with self.assertRaises(FenceRefused):owner.dispatch(os.getuid(),{**request,'path':'/override'})
                with self.assertRaises(FenceRefused):owner.dispatch(os.getuid(),{'operation':'status','seconds':1})
                result=probe_control(owner.path,request,{'pid':os.getpid(),'startTicks':probe_control(owner.path,{'operation':'status'})['peer']['startTicks']})
                self.assertTrue(result['receipt']['ok']);self.assertIn('qualification',result['receipt']['result'])
                time.sleep(1.05)
                current=probe_control(owner.path,{'operation':'status'})['receipt']['result'];self.assertTrue(current['drained']);self.assertEqual(current['holder'],'qualification')
            finally:owner.close()
