"""Configured measurement/retained-policy CPU controls; no grants, GPU or leases."""
import time
import unittest
from unittest.mock import Mock,patch
from livestack_node.hostbroker import HostBroker
from livestack_node.planner import Device,WorldState
from livestack_node.qualification_controller import QualificationController

class MeasurementTests(unittest.TestCase):
    def fixture(self):
        device=Device('gpu','host',{'vram_bytes':8192},reserved={'vram_bytes':1024})
        broker=HostBroker([device],[])
        controller=object.__new__(QualificationController);controller.broker=broker;controller._verify_peer=Mock()
        entry={'binding':{'deviceId':'gpu','hostId':'host','unitKind':'voxcpm'},'sourceIdentity':{'app':'polytts','configurationDigest':'config'},'clientSettings':{'serviceConfigurationPath':'/private/source.json'}}
        controller.nodes={'node':(entry,Mock(),None)}
        source={'apps':[{'descriptor':{'app':'polytts'},'providerQualificationNode':{'deviceId':'gpu','unitKind':'voxcpm','nvmlSha256':'a'*64}}]}
        world=WorldState(devices=(device,),units={},placements=(),requests=(),now=100,measured_free={})
        return controller,world,source

    def test_fresh_measurement_preserves_reserved_policy_without_claim_subtraction(self):
        controller,world,source=self.fixture()
        observation={'capacity':{'vram_bytes':8192},'free':{'vram_bytes':4096},'observedAt':time.monotonic()}
        with patch('livestack_node.qualification_controller.private_config',return_value=(source,'config')),patch('livestack_node.qualification_device.qualification_cuda_capacity',return_value=observation)as measure:
            actual=controller.measured_snapshot(world)
        self.assertEqual(actual.devices,world.devices);self.assertEqual(actual.devices[0].reserved,{'vram_bytes':1024})
        self.assertEqual(actual.measured_free,{'gpu':{'vram_bytes':4096}});self.assertEqual(world.measured_free,{})
        measure.assert_called_once_with('gpu','a'*64);self.assertEqual(controller._verify_peer.call_count,2)

    def test_changed_source_or_expired_measurement_refuses(self):
        controller,world,source=self.fixture()
        with patch('livestack_node.qualification_controller.private_config',return_value=(source,'changed')),patch('livestack_node.qualification_device.qualification_cuda_capacity')as measure:
            with self.assertRaisesRegex(PermissionError,'service_configuration_changed'):controller.measured_snapshot(world)
            measure.assert_not_called()
        old={'capacity':{'vram_bytes':8192},'free':{'vram_bytes':4096},'observedAt':time.monotonic()-21}
        with patch('livestack_node.qualification_controller.private_config',return_value=(source,'config')),patch('livestack_node.qualification_device.qualification_cuda_capacity',return_value=old):
            with self.assertRaisesRegex(PermissionError,'observation_expired'):controller.measured_snapshot(world)

    def test_declared_capacity_cannot_exceed_real_hardware(self):
        controller,world,source=self.fixture()
        small={'capacity':{'vram_bytes':4096},'free':{'vram_bytes':4096},'observedAt':time.monotonic()}
        with patch('livestack_node.qualification_controller.private_config',return_value=(source,'config')),patch('livestack_node.qualification_device.qualification_cuda_capacity',return_value=small):
            with self.assertRaisesRegex(PermissionError,'capacity_policy_refused'):controller.measured_snapshot(world)

    def test_unconfigured_broker_retains_default_snapshot(self):
        broker=HostBroker([Device('gpu','host',{'vram_bytes':8192})],[])
        with patch('livestack_node.qualification_device.qualification_cuda_capacity')as measure:
            world=broker.snapshot()
        self.assertEqual(len(world.devices),1);measure.assert_not_called()

if __name__=='__main__':unittest.main()
