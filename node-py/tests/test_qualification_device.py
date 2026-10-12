"""NVML parser/root executable pin CPU fixtures; no live CUDA or model effect."""
import hashlib
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock,patch
from livestack_node.qualification_device import qualification_cuda_device, qualification_cuda_capacity

class QualificationDeviceTests(unittest.TestCase):
    def test_actual_physical_index_uuid_hash_and_root_tool_identity_are_required(self):
        content=b'explicit CPU tool fixture';digest=hashlib.sha256(content).hexdigest()
        path=Mock();path.stat.return_value=SimpleNamespace(st_mode=0o100755,st_uid=0);path.read_bytes.return_value=content
        uuid='GPU-01234567-89ab-cdef-0123-456789abcdef';device='fixture/'+hashlib.sha256(uuid[4:].encode()).hexdigest()[:8]
        result=SimpleNamespace(stdout='0, GPU-11111111-1111-1111-1111-111111111111\n1, '+uuid+'\n')
        with patch('livestack_node.qualification_device.Path') as paths,patch('livestack_node.qualification_device.subprocess.run',return_value=result) as command,patch('livestack_node.qualification_device.socket.gethostname',return_value='fixture'):
            paths.return_value.resolve.return_value=path
            self.assertEqual(qualification_cuda_device('1',device,digest),device)
            with self.assertRaises(PermissionError):
                qualification_cuda_device('1','fixture/'+hashlib.sha256(uuid.encode()).hexdigest()[:8],digest)
            self.assertEqual(command.call_args.args[0][1:],['--query-gpu=index,uuid','--format=csv,noheader,nounits'])
            for index,expected,pin in (('0',device,digest),('1','wrong',digest),('1',device,'f'*64)):
                with self.assertRaises(PermissionError):qualification_cuda_device(index,expected,pin)
            path.stat.return_value=SimpleNamespace(st_mode=0o100777,st_uid=0)
            with self.assertRaises(PermissionError):qualification_cuda_device('1',device,digest)
    def test_missing_or_multidevice_selection_and_unknown_pin_refuse(self):
        for index,pin in ((None,'a'*64),('0,1','a'*64),('1','unknown')):
            with self.assertRaises(ValueError):qualification_cuda_device(index,'wrong',pin)

    def capacity_fixture(self,stack,stdout):
        content=b'CPU-only pinned telemetry executable';pin=hashlib.sha256(content).hexdigest();path=Mock()
        path.stat.return_value=SimpleNamespace(st_mode=0o100755,st_uid=0,st_dev=1,st_ino=2,st_mtime_ns=3,st_size=len(content));path.read_bytes.return_value=content
        paths=stack.enter_context(patch('livestack_node.qualification_device.Path'));paths.return_value.resolve.return_value=path
        run=stack.enter_context(patch('livestack_node.qualification_device.subprocess.run',return_value=SimpleNamespace(stdout=stdout)))
        stack.enter_context(patch('livestack_node.qualification_device.socket.gethostname',return_value='fixture'))
        uuid='GPU-01234567-89ab-cdef-0123-456789abcdef';device='fixture/'+hashlib.sha256(uuid[4:].encode()).hexdigest()[:8]
        return pin,device,path,run

    def test_capacity_units_real_uuid_and_zero_free_are_explicit(self):
        with ExitStack()as stack:
            pin,device,_,run=self.capacity_fixture(stack,'GPU-01234567-89ab-cdef-0123-456789abcdef, 8192, 0\n')
            result=qualification_cuda_capacity(device,pin)
            self.assertEqual(result['capacity'],{'vram_bytes':8192*1048576});self.assertEqual(result['free'],{'vram_bytes':0})
            self.assertEqual(run.call_args.args[0][1:],['--query-gpu=uuid,memory.total,memory.free','--format=csv,noheader,nounits'])
            with self.assertRaisesRegex(PermissionError,'actual_device_binding'):qualification_cuda_capacity('foreign/device',pin)

    def test_unknown_negative_overflow_and_impossible_capacity_refuse(self):
        for value in (None,'','GPU-01234567-89ab-cdef-0123-456789abcdef, 0, 0','GPU-01234567-89ab-cdef-0123-456789abcdef, 8192, 9000','GPU-01234567-89ab-cdef-0123-456789abcdef, 8192, -1','GPU-01234567-89ab-cdef-0123-456789abcdef, 8192, N/A','GPU-01234567-89ab-cdef-0123-456789abcdef, 8192.0, 1','GPU-01234567-89ab-cdef-0123-456789abcdef, '+('9'*40)+', 1'):
            with self.subTest(value=value),ExitStack()as stack:
                pin,device,_,_=self.capacity_fixture(stack,value)
                with self.assertRaises(ValueError):qualification_cuda_capacity(device,pin)

    def test_wrong_source_or_changed_tool_refuses_before_acceptance(self):
        with ExitStack()as stack:
            pin,device,path,run=self.capacity_fixture(stack,'GPU-01234567-89ab-cdef-0123-456789abcdef, 8192, 4096')
            with self.assertRaisesRegex(PermissionError,'nvml_executable_changed'):qualification_cuda_capacity(device,'f'*64)
            run.assert_not_called()
            original=path.stat.return_value
            path.stat.side_effect=[original,SimpleNamespace(**{**vars(original),'st_ino':4})]
            with self.assertRaisesRegex(PermissionError,'nvml_executable_changed'):qualification_cuda_capacity(device,pin)

if __name__=='__main__':unittest.main()
