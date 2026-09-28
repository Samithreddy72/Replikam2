"""Run the real read-only-write guard against a staged root, including its negative control."""
import os,pathlib,subprocess,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
class Preflight(unittest.TestCase):
 def test_forbidden_writes_fail_and_supported_data_paths_pass(self):
  text=(ROOT/'factory/preflight-check.sh').read_text();block=text[text.index('_writable='):]
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp);bin=root/'usr/local/bin';bin.mkdir(parents=True)
   script=bin/'sample';script.write_text('echo value > /etc/not-writable\n')
   # Redirect only this test's diagnostic scratch file; no host config is touched.
   block=block.replace('/tmp/_ro_writes.txt',str(root/'writes'))
   def run():return subprocess.run(['bash','-uc',block],env=dict(os.environ,ROOT=tmp),capture_output=True,text=True)
   self.assertNotEqual(run().returncode,0)
   script.write_text('echo value > /home/pi/flight.txt\necho value > /data/state\n')
   r=run();self.assertEqual(r.returncode,0,r.stdout+r.stderr)
if __name__=='__main__':unittest.main()
