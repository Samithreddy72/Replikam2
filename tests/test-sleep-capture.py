import pathlib,sys,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'app/netbridge-source'))
import source_app as app
from power_observer import dispatch,PowerObserver
class Sleep(unittest.TestCase):
 def tearDown(self):app._SUSPENDING.clear()
 def test_native_messages_stop_capture_and_wake_never_starts_it(self):
  for platform,sleep,wake in (('darwin',0xe0000280,0xe0000300),('win32',4,18)):
   with patch.object(app,'SESSION') as session,patch.object(app.threading,'Thread') as thread:
    session.wanted=True
    dispatch(platform,sleep,app.suspend_capture,app.resume_capture)
    self.assertFalse(session.wanted);session.stop.assert_called_once()
    self.assertTrue(app._SUSPENDING.is_set())
    dispatch(platform,wake,app.suspend_capture,app.resume_capture)
    self.assertFalse(app._SUSPENDING.is_set());session.start.assert_not_called()
    self.assertIn('Go live',session.interruption)
 def test_idle_sleep_does_not_send_session_commands(self):
  with patch.object(app,'SESSION') as session,patch.object(app.threading,'Thread') as thread:
   session.wanted=False;app.suspend_capture();thread.assert_not_called();session.stop.assert_not_called()
if __name__=='__main__':unittest.main()
