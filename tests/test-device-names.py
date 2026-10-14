#!/usr/bin/env python3
"""Non-ASCII Windows device names survive discovery, and names saved by older builds are repaired."""
import importlib.util,json,pathlib,sys,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app/netbridge-source'))
spec=importlib.util.spec_from_file_location('device_names',ROOT/'app/netbridge-source/source_app.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
MIC='Microphone Array (Intel® Smart Sound Technology for Digital Microphones)'
CAMERA='HP True Vision HD Camera'
MANGLED='Microphone Array (IntelÂ® Smart Sound Technology for Digital Microphones)'
STDERR=('[dshow @ 0] "%s" (video)\n[dshow @ 0]   Alternative name "@device_pnp_\\\\?\\usb"\n'
        '[dshow @ 0] "%s" (audio)\n' % (CAMERA,MIC)).encode('utf-8')
def windows_run(args,**kw):
  # ffmpeg writes UTF-8 to a pipe; text mode without an explicit encoding decodes it with the
  # ANSI code page, exactly as subprocess does on an English Windows machine.
  text=kw.get('text') or kw.get('encoding')
  return SimpleNamespace(returncode=1,stdout='',stderr=STDERR.decode(kw.get('encoding') or 'cp1252',kw.get('errors') or 'strict') if text else STDERR)
class DeviceNames(unittest.TestCase):
 def test_windows_discovery_keeps_the_real_microphone_name(self):
  with patch.object(a,'IS_WIN',True),patch.object(a,'_ffmpeg',return_value='ffmpeg.exe'),patch.object(a.subprocess,'run',side_effect=windows_run):
   d=a.av_devices()
  self.assertEqual([v['name'] for v in d['video']],[CAMERA])
  self.assertEqual([v['name'] for v in d['audio']],[MIC])
 def test_saved_mangled_names_are_repaired_on_load(self):
  with tempfile.TemporaryDirectory() as tmp:
   state=pathlib.Path(tmp)/'state.json';state.write_text(json.dumps({'mic_name':MANGLED,'camera_name':CAMERA,'token':'t'}))
   with patch.object(a,'STATE_FILE',state),patch.object(a,'LEGACY_DEVICE_ENCODING','cp1252'):
    st=a.load_state()
  self.assertEqual(st['mic_name'],MIC);self.assertEqual(st['camera_name'],CAMERA);self.assertEqual(st['token'],'t')
 def test_correct_names_are_left_alone(self):
  with patch.object(a,'LEGACY_DEVICE_ENCODING','cp1252'):
   for name in (MIC,CAMERA,'Réseau de microphones','マイク',None,''):
    self.assertEqual(a.repair_device_name(name),name)
if __name__=='__main__':unittest.main()
