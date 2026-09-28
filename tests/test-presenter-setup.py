"""Preflight and support reports use evidence without starting or reconfiguring media."""
import importlib.util,pathlib,sys,unittest
from unittest.mock import patch
from types import SimpleNamespace
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app/netbridge-source'))
spec=importlib.util.spec_from_file_location('presenter_setup',ROOT/'app/netbridge-source/source_app.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
class Setup(unittest.TestCase):
 def test_preflight_never_routes_and_does_not_claim_capture(self):
  state={'token':'private','bridge_id':'b','camera_name':'Camera','mic_name':'Mic'}
  with patch.object(a,'load_state',return_value=state),patch.object(a,'av_devices',return_value={'video':[{'name':'Camera'}],'audio':[{'name':'Mic'}]}),patch.object(a,'_fleet_bridges',return_value=[{'id':'b','online':True,'pin_protocol':2}]),patch.object(a.MESH,'route',side_effect=AssertionError('route changed')):
   r=a.setup_check({});checks={c['key']:c for c in r['checks']}
   self.assertEqual(checks['video']['status'],'pass');self.assertIn('permission',checks['video']['detail'])
   self.assertEqual(checks['receiver']['status'],'unknown');self.assertNotIn('private',str(r))
 def test_reports_mark_stale_evidence_unknown(self):
  with patch.object(a.BRIDGEWATCH,'snapshot',return_value={'age_s':99,'reachable':True,'checks':{'video_arriving':{'ok':True,'token':'private'}}}):
   r=a.support_report();self.assertIsNone(r['delivery']['video_arriving']);self.assertNotIn('private',str(r))
 def test_active_session_does_not_probe_capture_devices(self):
  with patch.object(a,'SESSION',SimpleNamespace(live=True)),patch.object(a,'load_state',return_value={}),patch.object(a,'av_devices',side_effect=AssertionError('live capture probed')):
   r=a.setup_check({});self.assertEqual(next(c for c in r['checks'] if c['key']=='video')['status'],'unknown')
if __name__=='__main__':unittest.main()
