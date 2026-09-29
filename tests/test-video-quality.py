import pathlib,sys,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'app/netbridge-source'))
import source_app as app
VideoQuality = app.VideoQuality
class Quality(unittest.TestCase):
 def test_transient_dip_and_missing_measurements_do_not_restart_video(self):
  q=VideoQuality()
  for value in (2,30,None,0,float('nan'),float('inf'),True,'12'):
   self.assertIsNone(q.observe(value,10))
  self.assertEqual(q.level,0)
 def test_sustained_low_rate_steps_down_with_cooldown(self):
  q=VideoQuality()
  self.assertIsNone(q.observe(10,0));self.assertIsNone(q.observe(10,10))
  self.assertEqual(q.observe(10,20),(20,'400k'))
  for t in (30,40,50,60,70):self.assertIsNone(q.observe(5,t))
  self.assertEqual(q.observe(5,80),(15,'250k'))
 def test_upgrade_requires_three_minutes_of_continuous_health(self):
  q=VideoQuality();q.level=1
  for t in (0,60,120,179):self.assertIsNone(q.observe(20,t))
  self.assertEqual(q.observe(20,180),(30,'600k'))
 def test_session_changes_only_video_and_preserves_usb_size(self):
  s=app.Session();s.wanted=True
  original=['ffmpeg','-vf','scale=424:240,format=nv12','-r','30','-g','30','-b:v','600k','rtp://example:5000']
  s.leg_argv={'video':original[:],'voice':['unchanged voice']}
  with patch.object(s,'respawn_leg',return_value=True) as restart,patch.object(s,'set_return') as audio:
   for t in (0,10,20):s.adapt_video({'source_state':'live','fps':10},t)
   restart.assert_called_once_with('video');audio.assert_not_called()
   self.assertEqual(s.leg_argv['voice'],['unchanged voice'])
   self.assertIn('scale=424:240,format=nv12',s.leg_argv['video'])
   self.assertIn('400k',s.leg_argv['video'])
 def test_stopped_and_frozen_sessions_never_adapt(self):
  s=app.Session()
  with patch.object(s,'respawn_leg') as restart:
   for t in range(10):s.adapt_video({'source_state':'live','fps':1},t)
   s.wanted=True
   for t in range(10):s.adapt_video({'source_state':'frozen','fps':1},t)
   restart.assert_not_called()
if __name__=='__main__':unittest.main()
