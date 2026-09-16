#!/usr/bin/env python3
import importlib.util,pathlib,tempfile,unittest,wave
import numpy as np
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('analyze',ROOT/'tools/analyze-usb-audio.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Analysis(unittest.TestCase):
 def test_detects_stale_request_data_and_preserves_channel_separation(self):
  rng=np.random.default_rng(123)
  clean=np.convolve(rng.normal(0,5000,96020),np.ones(15)/15,mode='valid')[:96000].astype('<i2')
  broken=clean.copy()
  for start in range(1536+48,len(broken)-16,96):broken[start:start+16]=broken[start-1536:start-1536+16]
  with tempfile.TemporaryDirectory() as td:
   p=pathlib.Path(td)/'test.wav'
   with wave.open(str(p),'wb') as w:
    w.setparams((2,2,48000,0,'NONE','not compressed'));w.writeframes(np.column_stack((clean,broken)).astype('<i2').tobytes())
   r=m.analyze(p)['channels_analysis'];self.assertLess(r[0]['recycle_exact_match_percent'],.1);self.assertGreater(r[1]['recycle_exact_match_percent'],10)
   self.assertGreater(r[1]['packet_boundary_energy_max_over_median'],r[0]['packet_boundary_energy_max_over_median']*5)
 def test_rejects_short_capture(self):
  with tempfile.TemporaryDirectory() as td:
   p=pathlib.Path(td)/'empty.wav'
   with wave.open(str(p),'wb') as w:w.setparams((1,2,48000,0,'NONE','not compressed'));w.writeframes(b'')
   with self.assertRaises(ValueError):m.analyze(p)
if __name__=='__main__':unittest.main()
