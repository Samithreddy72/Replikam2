"""An ahead/divergent or dirty deployed build must not be reported IN SYNC."""
import pathlib,tempfile,subprocess,unittest,shutil,json,os,sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
class Drift(unittest.TestCase):
 def test_deployed_identity_must_belong_to_checked_history(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);(root/'tools').mkdir();(root/'bin').mkdir();(root/'pi').mkdir();(root/'control-plane').mkdir()
   for name in ['fleet-drift-check.sh','control-plane-drift-check.sh']:shutil.copy(ROOT/'tools'/name,root/'tools'/name)
   def git(*args):return subprocess.check_output(['git',*args],cwd=root,text=True,stderr=subprocess.DEVNULL).strip()
   git('init');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
   (root/'pi/x').write_text('a');(root/'control-plane/x').write_text('a');git('add','.');git('commit','-qm','base');base=git('rev-parse','HEAD')
   (root/'pi/x').write_text('b');(root/'control-plane/x').write_text('b');git('add','.');git('commit','-qm','later');later=git('rev-parse','HEAD');git('checkout','--detach',base)
   fake=root/'bin/curl';fake.write_text('#!'+sys.executable+'\nimport os,sys\nprint("404" if "-w" in sys.argv else os.environ["FAKE_STATUS"])\n');fake.chmod(0o755)
   for sha,expected in [(base,0),(later,3),(base+'-dirty',3)]:
    for name in ['fleet-drift-check.sh','control-plane-drift-check.sh']:
     status={'version':'test-'+sha,'build':{'git_sha':sha},'git_sha':sha,'device_id':'fixture'}
     env=dict(os.environ,PATH=str(root/'bin')+os.pathsep+os.environ['PATH'],FAKE_STATUS=json.dumps(status))
     out=subprocess.run(['bash',str(root/'tools'/name)],env=env,capture_output=True,text=True)
     self.assertEqual(out.returncode,expected,(name,sha,out.stdout,out.stderr))
     if expected:self.assertNotIn('IN SYNC',out.stdout)
if __name__=='__main__':unittest.main()
