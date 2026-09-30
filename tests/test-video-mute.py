"""Camera-off starts without capture; switches/repairs affect only the video leg."""
import pathlib,sys,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'app/netbridge-source'))
import source_app as app
class VideoMute(unittest.TestCase):
 def setUp(self):
  self.session=app.Session();self.session.return_on=False;self.spawned=[]
  def spawn(argv,**kw):
   p=Mock();p.poll.return_value=None;p.argv=argv;self.spawned.append(p);return p
  for ctx in (patch.object(app,'_ffmpeg',return_value='ffmpeg'),patch.object(app,'_gst',return_value=None),patch.object(app.subprocess,'Popen',side_effect=spawn),patch.object(app,'_logdir',return_value=pathlib.Path('/tmp')),patch.object(app.PINS,'ticket',return_value='test-ticket'),patch.object(app,'_quit',side_effect=lambda p,**k:setattr(p.poll,'return_value',0))):
   ctx.start();self.addCleanup(ctx.stop)
  self.addCleanup(self.session.stop)
 def start(self,**kw):
  self.session.start('127.0.0.1','0','0',**kw);self.session.capture_names={'video':'Test camera','voice':'Test mic'}
 def test_camera_off_never_opens_capture_mac_or_windows(self):
  for win in (False,True):
   with patch.object(app,'IS_WIN',win),patch.object(app,'IS_MAC',not win):
    self.spawned.clear();self.start(video_muted=True,voice_muted=True)
    self.assertEqual(len(self.spawned),1);args=self.spawned[0].argv
    self.assertIn('lavfi',args);self.assertIn('-ssrc',args);self.assertNotIn('avfoundation',args);self.assertNotIn('dshow',args)
    self.assertTrue(self.session.video_muted);self.assertTrue(self.session.voice_muted);self.session.stop()
 def test_toggle_and_watchdog_never_reopen_muted_camera(self):
  self.start();voice=self.session.voice_proc;video=self.session.leg_proc['video']
  self.assertTrue(self.session.set_video_muted(True));self.assertEqual(video.poll(),0);self.assertIs(self.session.voice_proc,voice);self.assertIsNone(voice.poll())
  with patch.object(app,'av_devices',side_effect=AssertionError('must not enumerate/open camera')):
   self.assertTrue(self.session.respawn_leg('video'))
  self.assertIn('lavfi',self.session.leg_proc['video'].argv)
  with patch.object(app,'av_devices',return_value={'video':[{'name':'Test camera','index':'0'}]}):self.assertFalse(self.session.set_video_muted(False))
  self.assertNotIn('lavfi',self.session.leg_proc['video'].argv);self.assertIs(self.session.voice_proc,voice)
 def test_failed_unmute_keeps_camera_off(self):
  self.start(video_muted=True)
  with patch.object(app,'av_devices',return_value={'video':[]}):
   with self.assertRaises(RuntimeError):self.session.set_video_muted(False)
  self.assertTrue(self.session.video_muted);self.assertIn('lavfi',self.session.leg_proc['video'].argv)
 def test_stopped_session_cannot_toggle(self):
  with self.assertRaises(RuntimeError):self.session.set_video_muted(True)
 def test_start_muted_does_not_launch_microphone(self):
  self.start(voice_muted=True);self.assertEqual(len(self.spawned),1);self.assertFalse(self.session.voice_sending())
  self.assertFalse(self.session.set_voice_muted(False));self.assertTrue(self.session.voice_sending())
 def test_camera_off_disables_quality_adaptation(self):
  self.start(video_muted=True)
  with patch.object(self.session.video_quality,'observe') as observe:
   self.assertIsNone(self.session.adapt_video({'source_state':'live','fps':1},100));observe.assert_not_called()
if __name__=='__main__':unittest.main()
