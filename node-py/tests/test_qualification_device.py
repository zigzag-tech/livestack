"""NVML parser/root executable pin CPU fixtures; no live CUDA or model effect."""
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from livestack_node.qualification_device import qualification_cuda_device

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

if __name__=='__main__':unittest.main()
