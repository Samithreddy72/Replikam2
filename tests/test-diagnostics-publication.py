"""The diagnostic result cannot report success before the bundle is available."""
import ast,pathlib,unittest,tempfile,os,json,time,urllib.error
ROOT=pathlib.Path(__file__).resolve().parents[1]
class Publication(unittest.TestCase):
 def test_detached_result_is_uploaded_first_and_failure_is_visible(self):
  tree=ast.parse((ROOT/'pi/scripts/bridge-agent.py').read_text())
  funcs=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('collect_results','finish_diagnostics','command_result')]
  for uploaded in (True,False):
   with tempfile.TemporaryDirectory() as directory:
    p=pathlib.Path(directory)
    (p/'1.meta').write_text(json.dumps({'type':'diagnose','t':time.time()}));(p/'1.rc').write_text('0');(p/'1.out').write_text('collected')
    events=[]
    def upload(*args):events.append('upload');return uploaded
    def http(*args,**kwargs):events.append(kwargs['body'])
    ns=dict(os=os,json=json,time=time,urllib=urllib,RESULTS=directory,DETACHED={},upload_latest_bundle=upload,http=http,_forget=lambda cid:events.append('forget'))
    exec(compile(ast.Module(body=funcs,type_ignores=[]),'bridge-agent.py','exec'),ns)
    ns['collect_results']('https://fleet.example.com','test-token')
    self.assertEqual(events[0],'upload');self.assertEqual(events[1]['status'],'done' if uploaded else 'failed');self.assertEqual(events[2],'forget')
    if not uploaded:self.assertIn('upload failed',events[1]['output'])
if __name__=='__main__':unittest.main()
