"""Real signatures bind a script to its intended name and accepted revision."""
import importlib.util,json,pathlib,subprocess,tempfile,unittest,os
ROOT=pathlib.Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('signed_context',ROOT/'pi/scripts/bridge-verify-update.py');v=importlib.util.module_from_spec(s);s.loader.exec_module(v)
class Context(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=pathlib.Path(self.temp.name)
  self.key=self.root/'key';self.pub=self.root/'pub';self.payload=self.root/'script';self.sig=self.root/'sig';self.floors=self.root/'floors'
  subprocess.run(['openssl','ecparam','-name','prime256v1','-genkey','-noout','-out',str(self.key)],check=True,stderr=subprocess.DEVNULL)
  subprocess.run(['openssl','ec','-in',str(self.key),'-pubout','-out',str(self.pub)],check=True,stderr=subprocess.DEVNULL)
 def sign(self,revision=100,name='bridge-web.py',body='#!/bin/sh\necho OK\n'):
  src=self.root/'source';src.write_text(body);v.prepare(src,self.payload,name,revision)
  subprocess.run(['openssl','dgst','-sha256','-sign',str(self.key),'-out',str(self.sig),str(self.payload)],check=True)
 def verify(self,name='bridge-web.py',accept=False):return v.verify(self.payload,self.sig,self.pub,name,self.floors,accept)
 def test_identity_and_tampering(self):
  self.sign();self.assertEqual(self.verify()['revision'],100)
  with self.assertRaises(ValueError):self.verify('bridge-agent.py')
  self.payload.write_bytes(self.payload.read_bytes()+b'echo tampered\n')
  with self.assertRaises(ValueError):self.verify()
 def test_floor_survives_replay_and_conflicting_revision(self):
  self.sign();self.verify(accept=True);self.verify(accept=True)
  self.sign(99)
  with self.assertRaises(ValueError):self.verify()
  self.sign(100,body='#!/bin/sh\necho different\n')
  with self.assertRaises(ValueError):self.verify()
  self.sign(101);self.verify(accept=True)
  self.assertEqual(json.loads((self.floors/'bridge-web.py').read_text())['revision'],101)
 def test_corrupt_floor_and_signed_legacy_are_refused(self):
  self.sign();self.verify(accept=True);(self.floors/'bridge-web.py').write_text('broken')
  with self.assertRaises(ValueError):self.verify()
  self.payload.write_text('#!/bin/sh\necho legacy\n')
  subprocess.run(['openssl','dgst','-sha256','-sign',str(self.key),'-out',str(self.sig),str(self.payload)],check=True)
  with self.assertRaises(ValueError):self.verify()
 def test_prepare_preserves_shebang_and_cannot_insert_duplicate_context(self):
  self.sign();self.assertTrue(self.payload.read_bytes().startswith(b'#!/bin/sh\n# NetBridge-Update: '))
  src=self.root/'resign';src.write_bytes(self.payload.read_bytes());v.prepare(src,self.payload,'bridge-web.py',101)
  self.assertEqual(self.payload.read_bytes().count(v.PREFIX),1)
  with self.assertRaises(ValueError):v.prepare(src,self.payload,'../escape',102)
 def test_owner_rotation_survives_override_removal_and_refuses_old_keys(self):
  name='owner_ssh_authorized_keys';fallback=self.root/'baked-keys';fallback.write_bytes(b'old-key\n')
  self.assertEqual(v.owner_keys(self.pub,self.floors,fallback),b'old-key\n')
  self.sign(100,name,body='new-key\n');self.verify(name,accept=True)
  accepted=self.payload.read_bytes();self.payload.unlink();self.sig.unlink()
  self.assertEqual(v.owner_keys(self.pub,self.floors,fallback),accepted)
  self.sign(99,name,body='old-key\n')
  with self.assertRaises(ValueError):self.verify(name,accept=True)
  self.assertEqual(v.owner_keys(self.pub,self.floors,fallback),accepted)
 def test_owner_snapshot_loss_and_corrupt_floor_fail_closed(self):
  name='owner_ssh_authorized_keys';fallback=self.root/'baked-keys';fallback.write_bytes(b'old-key\n')
  self.sign(100,name,body='new-key\n');self.verify(name,accept=True)
  for snapshot in (self.floors/'.accepted'/name).iterdir():snapshot.unlink()
  with self.assertRaises(OSError):v.owner_keys(self.pub,self.floors,fallback)
  (self.floors/name).write_text('broken')
  with self.assertRaises(ValueError):v.owner_keys(self.pub,self.floors,fallback)
 def test_real_loader_checks_context_when_policy_enabled(self):
  self.sign(name='demo.sh');body=self.payload.read_bytes();sig=self.sig.read_bytes()
  baked=self.root/'baked';baked.mkdir();(baked/'demo.sh').write_text('#!/bin/sh\necho BAKED\n');(baked/'demo.sh').chmod(0o755)
  data=self.root/'overrides';data.mkdir();(data/'demo.sh').write_bytes(body);(data/'demo.sh').chmod(0o755);(data/'demo.sh.sig').write_bytes(sig)
  loader=(ROOT/'pi/scripts/bridge-run.sh').read_text().replace('BAKED="/usr/local/bin/$NAME"',f'BAKED="{baked}/$NAME"').replace('DIR="/data/overrides"',f'DIR="{data}"').replace('STATE="/data/overrides/.state"',f'STATE="{data}/.state"').replace('PUBKEY="/etc/netbridge/script-pubkey.pem"',f'PUBKEY="{self.pub}"')
  run=self.root/'loader';run.write_text(loader);ctl=self.root/'systemctl';ctl.write_text('#!/bin/sh\necho 0\n');ctl.chmod(0o755);policy=self.root/'policy';policy.touch()
  env=dict(os.environ,BRIDGE_SIGNATURE_POLICY=str(policy),BRIDGE_SIGNATURE_VERIFIER=str(ROOT/'pi/scripts/bridge-verify-update.py'),BRIDGE_RUN_SYSTEMCTL=str(ctl))
  self.assertEqual(subprocess.check_output(['bash',str(run),'demo.sh'],env=env,stderr=subprocess.DEVNULL,text=True).strip(),'OK')
  # Same valid signature, moved under another allowed-looking name: never executed.
  (baked/'other.sh').write_bytes((baked/'demo.sh').read_bytes());(baked/'other.sh').chmod(0o755)
  (data/'other.sh').write_bytes(body);(data/'other.sh').chmod(0o755);(data/'other.sh.sig').write_bytes(sig)
  self.assertEqual(subprocess.check_output(['bash',str(run),'other.sh'],env=env,stderr=subprocess.DEVNULL,text=True).strip(),'BAKED')
if __name__=='__main__':unittest.main()
