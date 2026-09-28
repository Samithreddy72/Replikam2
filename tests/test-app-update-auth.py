"""Real signature checks on staged updates, including local metadata substitution."""
import pathlib,sys,tempfile,unittest,subprocess,json,hashlib,os
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app/netbridge-source'))
import source_app as a
class Updates(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name)
  self.exe=self.root/'NetBridgeSource';self.exe.write_bytes(b'old-binary')
  self.key=self.root/'key.pem';self.pub=self.root/'pub.pem'
  subprocess.run(['openssl','ecparam','-name','prime256v1','-genkey','-noout','-out',str(self.key)],check=True,capture_output=True)
  subprocess.run(['openssl','pkey','-in',str(self.key),'-pubout','-out',str(self.pub)],check=True,capture_output=True)
  self.exe.with_suffix('.new').write_bytes(b'new-binary')
  self.man=self.exe.with_suffix('.new.manifest');self.sig=self.exe.with_suffix('.new.sig')
  self.man.write_text('version=1.5.2\nfile=NetBridgeSource\nsha256='+hashlib.sha256(b'new-binary').hexdigest()+'\n')
  subprocess.run(['openssl','dgst','-sha256','-sign',str(self.key),'-out',str(self.sig),str(self.man)],check=True,capture_output=True)
  self.exe.with_suffix('.new.json').write_text(json.dumps({'version':'9.9.9','sha256':hashlib.sha256(b'evil').hexdigest()}))
  for name,value in [('_update_compatible',lambda fields:True),('_app_binary',lambda:self.exe),('_update_pubkey',lambda:str(self.pub)),('load_state',lambda:{}),('save_state',lambda state:None)]:
   p=patch.object(a,name,value);p.start();self.addCleanup(p.stop)
 def test_unsigned_metadata_cannot_authorize_replaced_binary(self):
  self.exe.with_suffix('.new').write_bytes(b'evil')
  with patch.object(a.os,'execve') as launch:a.apply_staged_update();launch.assert_not_called()
  self.assertEqual(self.exe.read_bytes(),b'old-binary')
 def test_missing_or_tampered_signature_cannot_install(self):
  self.sig.write_bytes(b'invalid')
  with patch.object(a.os,'execve') as launch:a.apply_staged_update();launch.assert_not_called()
  self.assertEqual(self.exe.read_bytes(),b'old-binary')
 def test_valid_manifest_not_unsigned_version_controls_install_and_env(self):
  with patch.dict(os.environ,{'_PYI_APPLICATION_HOME_DIR':'old-runtime','_MEIPASS2':'old'}),patch.object(a.os,'execve',side_effect=SystemExit(0)) as launch,patch.object(a,'save_state') as state:
   with self.assertRaises(SystemExit):a.apply_staged_update()
   self.assertEqual(state.call_args.args[0]['updated_to'],'1.5.2')
   env=launch.call_args.args[2];self.assertEqual(env['PYINSTALLER_RESET_ENVIRONMENT'],'1');self.assertNotIn('_MEIPASS2',env);self.assertNotIn('_PYI_APPLICATION_HOME_DIR',env)
  self.assertEqual(self.exe.read_bytes(),b'new-binary')
 def test_launch_failure_restores_original(self):
  with patch.object(a.os,'execve',side_effect=OSError('cannot execute')):a.apply_staged_update()
  self.assertEqual(self.exe.read_bytes(),b'old-binary')
 def test_bundled_verifier_does_not_need_openssl(self):
  import cryptography
  with patch.object(a.shutil,'which',return_value=None),patch.object(a.subprocess,'run',side_effect=AssertionError('external verifier used')):
   self.assertTrue(a._verify_sig(self.pub,self.sig,self.man))
   self.man.write_text('tampered');self.assertFalse(a._verify_sig(self.pub,self.sig,self.man))
 def test_path_escape_duplicate_and_bad_digest_rejected(self):
  for raw in ('version=1.5.2\nfile=../escape\nsha256='+'a'*64,'version=1.5.2\nversion=1.5.3\nfile=app\nsha256='+'a'*64,'version=1.5.2\nfile=app\nsha256=bad'):
   self.man.write_text(raw);self.assertIsNone(a._manifest_fields(self.man))
class BundleSafety(unittest.TestCase):
 def test_native_bundle_is_never_replaced_by_bare_executable(self):
  with patch.object(a.sys,'frozen',True,create=True):
   for path in ('/Applications/NetBridge.app/Contents/MacOS/NetBridgeSource','/Applications/NetBridge.APP/Contents/MacOS/NetBridgeSource'):
    with patch.object(a.sys,'executable',path):self.assertIsNone(a._app_binary())
   with patch.object(a.sys,'executable','/tmp/NetBridgeSource'):
    self.assertEqual(a._app_binary(),pathlib.Path('/tmp/NetBridgeSource').resolve())

class Compatibility(unittest.TestCase):
 def setUp(self):
  for name,value in [('_mesh_bin',lambda:'test-helper'),('_sha256',lambda path:'a'*64)]:
   p=patch.object(a,name,value);p.start();self.addCleanup(p.stop)
 def test_updates_require_matching_platform_protocol_and_fresh_selected_bridge(self):
  fields={'platform':a._plat_tag(),'bridge_protocol':'2','helper_sha256':'a'*64}
  with patch.object(a,'load_state',return_value={'bridge_id':'b','token':'test'}),patch.object(a,'_fleet_bridges',return_value=[{'id':'b','online':True,'pin_protocol':2}]):
   self.assertTrue(a._update_compatible(fields))
   self.assertFalse(a._update_compatible(dict(fields,helper_sha256='b'*64)))
   self.assertFalse(a._update_compatible(dict(fields,helper_sha256=None)))
   self.assertFalse(a._update_compatible(dict(fields,platform='wrong')))
   self.assertFalse(a._update_compatible(dict(fields,bridge_protocol='1')))
  for bridge in ({'id':'b','online':True,'pin_protocol':1},{'id':'b','online':False,'pin_protocol':2},{'id':'other','online':True,'pin_protocol':2},{}):
   with patch.object(a,'load_state',return_value={'bridge_id':'b','token':'test'}),patch.object(a,'_fleet_bridges',return_value=[bridge]):self.assertFalse(a._update_compatible(fields))
if __name__=='__main__':unittest.main()
