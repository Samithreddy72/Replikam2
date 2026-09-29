import pathlib,os,subprocess,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
class Recovery(unittest.TestCase):
 def run_command(self, supported, fail=False):
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d);marker=root/'supported';calls=root/'calls';binary=root/'bin';binary.mkdir()
   if supported:marker.write_text('1')
   script=root/'bridge';script.write_text((ROOT/'pi/scripts/bridge').read_text().replace('/etc/netbridge/video-fallback-v1',str(marker)))
   sudo=binary/'sudo';sudo.write_text('#!/bin/sh\nexec "$@"\n');sudo.chmod(0o755)
   systemctl=binary/'systemctl';systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\nexit "${FAIL:-0}"\n');systemctl.chmod(0o755)
   p=subprocess.run(['bash',str(script),'recover-video'],env={**os.environ,'PATH':str(binary)+':'+os.environ['PATH'],'CALLS':str(calls),'FAIL':'1' if fail else '0'},capture_output=True,text=True)
   return p,calls.read_text().splitlines() if calls.exists() else []
 def test_restarts_only_receiver_and_keeps_camera_audio_services(self):
  p,calls=self.run_command(True)
  self.assertEqual(p.returncode,0,p.stderr)
  self.assertEqual(calls,['reset-failed bridge-feeder-net','restart bridge-feeder-net','is-active --quiet bridge-feeder-net'])
 def test_unsupported_image_is_not_modified(self):
  p,calls=self.run_command(False);self.assertNotEqual(p.returncode,0);self.assertEqual(calls,[])
 def test_command_failure_is_not_reported_as_recovered(self):
  p,calls=self.run_command(True,True);self.assertNotEqual(p.returncode,0)
  self.assertNotIn('Video receiver restarted',p.stdout)
if __name__=='__main__':unittest.main()
