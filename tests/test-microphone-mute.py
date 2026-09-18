#!/usr/bin/env python3
"""Muting must survive automatic repairs, and unmute failure must stay muted."""
import pathlib,sys,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'app/netbridge-source'))
import source_app as app
class MuteTests(unittest.TestCase):
 def setUp(self):
  self.session=app.Session();self.session.wanted=True;self.session.return_on=False
  self.voice=Mock();self.voice.poll.return_value=None
  self.session.leg_proc={'voice':self.voice};self.session.procs=[self.voice];self.session.voice_proc=self.voice
 def test_mute_stops_only_voice_and_prevents_respawn(self):
  with patch.object(app,'_quit', side_effect=lambda p:setattr(p.poll,'return_value',1)) as quit:
   self.assertTrue(self.session.set_voice_muted(True));quit.assert_called_once_with(self.voice)
  self.assertIsNone(self.session.leg_status()['voice'])
  with patch.object(app.subprocess,'Popen') as spawn:
   self.assertFalse(self.session.respawn_leg('voice'));spawn.assert_not_called()
 def test_unmute_failure_remains_muted(self):
  self.session.voice_muted=True;self.voice.poll.return_value=1
  with patch.object(self.session,'respawn_leg',return_value=False):
   with self.assertRaises(RuntimeError):self.session.set_voice_muted(False)
  self.assertTrue(self.session.voice_muted)
 def test_stopped_session_cannot_unmute(self):
  self.session.wanted=False
  with self.assertRaises(RuntimeError):self.session.set_voice_muted(False)
 def test_guard_does_not_restart_muted_microphone(self):
  self.session.voice_muted=True;self.voice.poll.return_value=1
  # Keep the video leg alive, otherwise the whole session is idle.
  video=Mock();video.poll.return_value=None;self.session.procs.append(video)
  guard=app.StreamGuard();guard.GRACE_S=0
  with patch.object(app,'SESSION',self.session),patch.object(self.session,'respawn_leg') as restart:
   guard._tick();restart.assert_not_called()
if __name__=='__main__':unittest.main()
