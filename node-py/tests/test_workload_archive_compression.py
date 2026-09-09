"""Actual gzip/tar capture and extraction, including expanded-byte limits."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from livestack_node.workloads.archive import capture, unpack
from livestack_node.workloads.model import WorkloadError


class CompressedArchiveTest(unittest.TestCase):
    def test_deterministic_compressed_and_legacy_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'source'; source.mkdir()
            (source/'code').write_bytes(b'repeated source line\n'*50000)
            (source/'code').chmod(0o755)
            outputs = []
            for index in range(2):
                bundle = root/f'{index}.tar.gz'
                result = capture(source, ['code'], bundle, compression='gzip')
                outputs.append(result['digest'])
                self.assertLess(result['size'], 20000)
                unpack(bundle, root/f'unpacked-{index}', result['digest'])
                self.assertEqual((root/f'unpacked-{index}/code').read_bytes(), (source/'code').read_bytes())
                self.assertEqual((root/f'unpacked-{index}/code').stat().st_mode & 0o777, 0o755)
            self.assertEqual(outputs[0], outputs[1])
            with self.assertRaisesRegex(WorkloadError, 'expanded source bound'):
                unpack(root/'0.tar.gz', root/'too-large', outputs[0], max_bytes=100000)
            self.assertFalse((root/'too-large').exists())
            legacy = capture(source, ['code'], root/'raw.tar')
            unpack(root/'raw.tar', root/'legacy', legacy['digest'])
            self.assertEqual((root/'legacy/code').read_bytes(), (source/'code').read_bytes())
            with self.assertRaisesRegex(WorkloadError, 'source byte limit'):
                capture(source, ['code'], root/'too-small.gz', compression='gzip', max_bytes=100000)
            self.assertFalse((root/'too-small.gz').exists())
            with self.assertRaisesRegex(WorkloadError, 'unsupported source compression'):
                capture(source, ['code'], root/'bad', compression='xz')
            # Hash validation still covers encoded transport bytes, not just
            # the decompressed contents; changing a gzip header is detectable.
            damaged = bytearray((root/'0.tar.gz').read_bytes()); damaged[4] ^= 1
            (root/'damaged.gz').write_bytes(damaged)
            with self.assertRaisesRegex(WorkloadError, 'digest mismatch'):
                unpack(root/'damaged.gz', root/'damaged', outputs[0])
