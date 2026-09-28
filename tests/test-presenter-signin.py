"""Execute presenter sign-in handlers with controlled Fleet failures and fresh state."""
import ast,pathlib,unittest,urllib.parse,types
ROOT=pathlib.Path(__file__).resolve().parents[1]
TREE=ast.parse((ROOT/'app/netbridge-source/source_app.py').read_text())
class Signin(unittest.TestCase):
 def setUp(self):
  names={'_signin_url','_signin_error','_redeem_signin','_bridge_rec'}
  nodes=[n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name in names]
  handler=next(n for n in TREE.body if isinstance(n,ast.ClassDef) and n.name=='Handler')
  nodes += [n for n in handler.body if isinstance(n,ast.FunctionDef) and n.name=='do_POST']
  self.state={};self.saved=[];self.calls=[];self.replies=[]
  def api(method,url,**kwargs):
   self.calls.append((method,url,kwargs));return self.replies.pop(0)
  self.ns={'urllib':urllib,'api':api,'load_state':lambda:dict(self.state),'save_state':lambda s:self.saved.append(dict(s)), '_BRIDGES':{'list':[]}}
  exec(compile(ast.Module(body=nodes,type_ignores=[]),'source_app.py','exec'),self.ns)
 def post(self,path,body):
  h=types.SimpleNamespace(path=path,_local_host_ok=lambda:True,_csrf_ok=lambda:True,_body=lambda:body,_send=lambda r,status=200:(status,r))
  return self.ns['do_POST'](h)
 def test_normalizes_bare_fleet_and_rejects_insecure_or_malformed(self):
  self.assertEqual(self.ns['_signin_url'](' fleet.example.com/ '),'https://fleet.example.com')
  for url in ['', 'http://fleet.example.com','https://user:pass@fleet.example.com','https://fleet.example.com/?token=x','https://fleet.example.com:bad']:
   with self.assertRaises(ValueError):self.ns['_signin_url'](url)
 def test_failed_request_does_not_claim_email_sent_or_save_bad_address(self):
  self.replies=[{'_error':'<urlopen error name lookup failed>'}]
  status,r=self.post('/api/signin-request',{'control_url':'bad.example.com','email':'a@example.com'})
  self.assertEqual(status,502);self.assertIn('Cannot reach Fleet',r['_error']);self.assertEqual(self.saved,[])
 def test_success_preserves_non_enumerating_message(self):
  self.replies=[{'ok':True}]
  status,r=self.post('/api/signin-request',{'control_url':'fleet.example.com','email':'a@example.com'})
  self.assertEqual(status,200);self.assertIn('If that address has an account',r['note']);self.assertEqual(self.saved[0]['control_url'],'https://fleet.example.com')
 def test_fresh_install_invite_uses_form_url(self):
  self.replies=[{'_error':'invalid code','_code':401},{'token':'test-token','email':'a@example.com'},{'email':'a@example.com'}]
  status,r=self.post('/api/signin-redeem',{'control_url':'fleet.example.com','code':'invite'})
  self.assertEqual(status,200);self.assertEqual(self.saved[0]['token'],'test-token');self.assertEqual(len(self.calls),3)
 def test_expired_code_gives_helpful_message(self):
  self.state={'control_url':'https://fleet.example.com'};self.replies=[{'_error':'expired','_code':401},{'_error':'invalid invite','_code':401}]
  status,r=self.post('/api/signin-redeem',{'code':'expired'})
  self.assertEqual(status,401);self.assertIn('Request a new code',r['_error']);self.assertEqual(self.saved,[])
 def test_outage_does_not_attempt_invite_redemption(self):
  self.state={'control_url':'https://fleet.example.com'};self.replies=[{'_error':'network failure'}]
  status,r=self.post('/api/signin-redeem',{'code':'code'})
  self.assertEqual(status,502);self.assertEqual(len(self.calls),1);self.assertIn('Cannot reach Fleet',r['_error'])
 def test_device_id_resolves_to_cached_network_addresses(self):
  self.ns['_BRIDGES']['list']=[{'id':'device-123','tailscale_ip':'100.64.1.2','latest':{'ip':'192.168.1.2'}}]
  self.assertEqual(self.ns['_bridge_rec']('device-123',{})['tailscale_ip'],'100.64.1.2')
if __name__=='__main__':unittest.main()
