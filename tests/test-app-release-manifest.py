"""The build feed and signed metadata must match the updater's acceptance contract."""
import importlib.util, pathlib, tempfile, unittest, hashlib
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('source_build',ROOT/'app/netbridge-source/build.py')
build=importlib.util.module_from_spec(spec);spec.loader.exec_module(build)
class Manifest(unittest.TestCase):
 def test_protocol_feed_binds_actual_app_and_helper(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);app=root/'app';helper=root/'helper'
   app.write_bytes(b'app');helper.write_bytes(b'helper')
   with patch.object(build,'DIST',root/'dist'):
    rel,man=build.write_update_manifest(app,helper,'1.5.2','NetBridgeSource')
   fields=dict(line.split('=',1) for line in man.read_text().splitlines())
   self.assertEqual(rel.parent.name,'pin-v2');self.assertEqual(fields['platform'],build._plat_tag())
   self.assertEqual(fields['bridge_protocol'],'2')
   self.assertEqual(fields['sha256'],hashlib.sha256(b'app').hexdigest())
   self.assertEqual(fields['helper_sha256'],hashlib.sha256(b'helper').hexdigest())
   self.assertEqual((rel/fields['file']).read_bytes(),b'app')
 def test_missing_helper_or_invalid_version_cannot_publish(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);app=root/'app';app.write_bytes(b'app')
   with patch.object(build,'DIST',root/'dist'):
    for version in ('1.5.2','../escape'):
     with self.assertRaises(ValueError):build.write_update_manifest(app,root/'absent',version,'NetBridgeSource')
   self.assertFalse((root/'dist').exists())
if __name__=='__main__':unittest.main()
