"""A release-supplied public key must never authorize the flash candidate."""
import os,pathlib,subprocess,tempfile,unittest,hashlib
ROOT=pathlib.Path(__file__).resolve().parents[1]
class Trust(unittest.TestCase):
 def test_wrong_signer_and_wrong_release_leave_flash_folder_unchanged(self):
  with tempfile.TemporaryDirectory() as tmp:
   t=pathlib.Path(tmp);dest=t/'dest';dest.mkdir();old=dest/'1--FLASH-THIS--old.img.xz';old.write_bytes(b'known-good')
   release=t/'release';release.mkdir();bin=t/'bin';bin.mkdir()
   gh=bin/'gh';gh.write_text('#!/bin/bash\nwhile [ "$#" -gt 0 ]; do if [ "$1" = -D ]; then cp "$TEST_RELEASE"/* "$2"; exit 0; fi; shift; done\nexit 1\n');gh.chmod(0o755)
   def run(*args):return subprocess.run(args,capture_output=True,text=True,check=True)
   for name in ('owner','wrong'):
    run('openssl','ecparam','-name','prime256v1','-genkey','-noout','-out',str(t/(name+'.key')))
    run('openssl','pkey','-in',str(t/(name+'.key')),'-pubout','-out',str(t/(name+'.pub')))
   image=release/'netbridge-os-test.img.xz';image.write_bytes(b'test-image')
   manifest=release/'manifest-disk.txt';sig=release/'manifest-disk.txt.sig'
   # Simulate an attacker also placing their public key beside their image.
   (release/'ota-pubkey.pem').write_bytes((t/'wrong.pub').read_bytes())
   env=dict(os.environ,PATH=str(bin)+os.pathsep+os.environ['PATH'],TEST_RELEASE=str(release),NETBRIDGE_IMAGE_DIR=str(dest),NETBRIDGE_IMAGE_TAG='v2.2.0-abcdef1',NETBRIDGE_OTA_PUBKEY=str(t/'owner.pub'))
   for signer,version in [('wrong','2.2.0-abcdef1'),('owner','wrong-release')]:
    manifest.write_text('version='+version+'\nsha256='+hashlib.sha256(image.read_bytes()).hexdigest()+'\n')
    run('openssl','dgst','-sha256','-sign',str(t/(signer+'.key')),'-out',str(sig),str(manifest))
    p=subprocess.run(['bash',str(ROOT/'tools/stage-image.sh'),'abcdef1'],env=env,capture_output=True,text=True)
    self.assertNotEqual(p.returncode,0);self.assertNotIn('3/5',p.stdout);self.assertEqual(old.read_bytes(),b'known-good');self.assertEqual(len(list(dest.iterdir())),1)
if __name__=='__main__':unittest.main()
