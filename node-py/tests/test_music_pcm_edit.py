import tempfile
import unittest
from pathlib import Path
import numpy as np
from livestack_node.music.pcm_edit import prepare_gap, read_pcm, splice, write_pcm

class MusicPcmEditTests(unittest.TestCase):
 def setUp(self):
  self.directory=tempfile.TemporaryDirectory();self.root=Path(self.directory.name)
 def tearDown(self):self.directory.cleanup()
 def test_gap_moves_suffix_without_stretching_samples(self):
  p=self.root;original=np.arange(2000,dtype='<i2').reshape(-1,2);write_pcm(p/'source.wav',44100,original)
  result=prepare_gap(p/'source.wav',200,400,500,p/'gap.wav');_,actual=read_pcm(p/'gap.wav')
  self.assertEqual(len(actual),1300);self.assertEqual(result['shiftFrames'],300)
  self.assertTrue(np.array_equal(actual[:200],original[:200]));self.assertFalse(actual[200:700].any());self.assertTrue(np.array_equal(actual[700:],original[400:]))
 def test_splice_changes_only_mask_with_inside_blends(self):
  p=self.root;original=np.arange(2000,dtype='<i2').reshape(-1,2);write_pcm(p/'source.wav',44100,original);write_pcm(p/'candidate.wav',44100,-original)
  result=splice(p/'source.wav',p/'candidate.wav',200,600,p/'out.wav',30);_,actual=read_pcm(p/'out.wav')
  self.assertTrue(result['outsidePcmExact']);self.assertTrue(np.array_equal(actual[:200],original[:200]));self.assertTrue(np.array_equal(actual[600:],original[600:]));self.assertTrue(np.array_equal(actual[230:570],-original[230:570]))
 def test_invalid_bounds_refuse_before_output(self):
  p=self.root;write_pcm(p/'source.wav',44100,np.zeros((1000,2),dtype='<i2'))
  for start,end,target in [(True,400,100),(-1,400,100),(200,1200,100),(200,100,10),(200,400,-1)]:
   with self.subTest(bounds=(start,end,target)):
    with self.assertRaises(ValueError):prepare_gap(p/'source.wav',start,end,target,p/'bad.wav')
    self.assertFalse((p/'bad.wav').exists())
if __name__=='__main__':unittest.main()
