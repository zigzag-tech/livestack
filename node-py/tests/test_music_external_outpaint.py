import unittest
from livestack_node.music.external_worker import validate_request, MODELS

class OutpaintContractTests(unittest.TestCase):
 def body(self):
  return dict(jobId='continuation',ownerId='owner',model='stable-audio-3-outpaint',prompt='Continue the same string theme and percussion.',seconds=60,outputFormat="wav",seed=42,sourceSha256='a'*64,sampleRate=44100,sourceFrames=24*44100)
 def test_existing_source_clock_and_bounded_extension(self):
  body=self.body();self.assertEqual(validate_request(body,'owner'),body)
  self.assertEqual(MODELS[body['model']],'fal-ai/stable-audio-3/medium/audio-outpainting')
 def test_long_continuation_uses_compressed_transfer(self):
  body={**self.body(),"seconds":196,"outputFormat":"mp3"};self.assertEqual(validate_request(body,"owner"),body)
 def test_refuses_ambiguous_clock_or_unbounded_extension(self):
  for change in [dict(sourceFrames=True),dict(seconds=24),dict(seconds=301),dict(seconds=196),dict(outputFormat="unknown"),dict(sampleRate=22050),dict(sourceSha256='bad'),dict(startFrame=0)]:
   with self.subTest(change=change):
    with self.assertRaises(ValueError):validate_request({**self.body(),**change},'owner')
if __name__=='__main__':unittest.main()
