"""Actual entry mapper/private files, explicit mock canonical/kernel/manager gate."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from livestack_node.provider_entry import configure_qualification
from livestack_node.provider_installation import sha
from livestack_node.provider_owner_probe import process_start_ticks

class QualificationEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'owner.json'
        self.node=dict(nodeId='node',authority='fixture-authority',deviceId='gpu',unitKind='voxcpm',hostId='fixture',capacityUrl='http://127.0.0.1:8799/qualification/admit',reservationPath=str(self.root/'reservations.sqlite'),ownerSocket=str(self.root/'owner.sock'),unitSha256='a'*64,nvmlSha256='f'*64,activationStore=str(self.root/'activation.json'),authorizationClient={'serviceConfigurationPath':str(self.path)})
        self.write({'apps':[{'descriptor':{'app':'polytts'},'providerQualificationNode':self.node}]})
        self.owner=SimpleNamespace(instance_id='b'*32);self.module=SimpleNamespace(manager=object(),configure_provider_qualification=Mock())
        self.settings={'qualificationOnly':True,'controlSocket':self.node['ownerSocket']}
    def write(self,value):
        self.path.write_text(json.dumps(value));self.path.chmod(0o600)
        self.identity={'app':'polytts','configurationDigest':sha(self.path.read_bytes())}
    def tearDown(self):self.tmp.cleanup()
    def test_absent_mode_is_default_noop_and_malformed_mode_refuses(self):
        self.assertIsNone(configure_qualification('/nonexistent','polytts',None,None,{},None))
        with self.assertRaises(ValueError):configure_qualification('/nonexistent','polytts',None,None,{'qualificationOnly':'true'},None)
    def test_mapper_derives_current_boot_process_source_and_owner_instance(self):
        client=SimpleNamespace(source_digest='c'*64)
        with patch('livestack_node.provider_qualification.QualificationClient',return_value=client),patch('livestack_node.qualification_node.QualificationNodeGate') as gate,patch.dict(os.environ,{'INVOCATION_ID':'d'*32}):
            result=configure_qualification(str(self.path),'polytts',self.module,self.owner,self.settings,self.identity)
        args,kwargs=gate.call_args
        binding=args[3]
        self.assertEqual(binding['serverInstanceId'],self.owner.instance_id)
        self.assertEqual(binding['processStartTicks'],str(process_start_ticks(os.getpid())))
        self.assertEqual(binding['bootId'],Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        self.assertEqual(binding['configurationSha256'],self.identity['configurationDigest'])
        self.assertEqual(kwargs['owner_socket'],self.node['ownerSocket'])
        self.module.configure_provider_qualification.assert_called_once_with(result)
    def test_changed_private_constraints_wrong_socket_and_missing_actual_invocation_refuse(self):
        old=dict(self.identity);self.path.write_text('{"changed":true}')
        with self.assertRaises(ValueError):configure_qualification(str(self.path),'polytts',self.module,self.owner,self.settings,old)
        self.node['ownerSocket']='wrong';self.write({'apps':[{'descriptor':{'app':'polytts'},'providerQualificationNode':self.node}]})
        with self.assertRaises(ValueError):configure_qualification(str(self.path),'polytts',self.module,self.owner,self.settings,self.identity)
        self.node['ownerSocket']=self.settings['controlSocket'];self.write({'apps':[{'descriptor':{'app':'polytts'},'providerQualificationNode':self.node}]})
        with patch('livestack_node.provider_qualification.QualificationClient',return_value=SimpleNamespace(source_digest='c'*64)),patch.dict(os.environ,{'INVOCATION_ID':''}):
            with self.assertRaises(ValueError):configure_qualification(str(self.path),'polytts',self.module,self.owner,self.settings,self.identity)
        self.module.configure_provider_qualification.assert_not_called()

if __name__=='__main__':unittest.main()
