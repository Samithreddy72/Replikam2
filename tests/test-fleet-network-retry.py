"""Enrollment remains automatic; network events target only the bounded agent."""
import importlib.util,json,pathlib,tempfile,unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('agent',ROOT/'pi/scripts/bridge-agent.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
class Retry(unittest.TestCase):
 def test_replacement_enrollment_needs_no_recovery_file(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(a,'STATE_DIR',tmp),patch.object(a,'TOKEN_FILE',tmp+'/token'),patch.object(a,'http',return_value={'device_token':'issued'}) as http:
   tel=dict(device_id='bridge',pairing_code='test',version='test',tailscale_ip='',host='bridge')
   self.assertEqual(a.enroll('https://fleet.test',{'BOOTSTRAP_TOKEN':'factory'},tel),'issued')
   self.assertNotIn('recovery_token',http.call_args.kwargs['body'])
   self.assertEqual(a.enroll('https://fleet.test',{},tel),'issued')
   http.assert_called_once()
 def test_failed_request_can_retry_without_partial_credential(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(a,'STATE_DIR',tmp),patch.object(a,'TOKEN_FILE',tmp+'/token'),patch.object(a,'http',side_effect=[OSError('offline'),{'device_token':'issued'}]):
   tel=dict(device_id='bridge',pairing_code='test',version='test',tailscale_ip='',host='bridge')
   with self.assertRaises(OSError):a.enroll('https://fleet.test',{'BOOTSTRAP_TOKEN':'factory'},tel)
   self.assertFalse(pathlib.Path(tmp+'/token').exists())
   self.assertEqual(a.enroll('https://fleet.test',{'BOOTSTRAP_TOKEN':'factory'},tel),'issued')
 def test_timer_retries_after_completion_and_boots_promptly(self):
  text=(ROOT/'pi/systemd/bridge-agent.timer').read_text()
  self.assertIn('OnUnitInactiveSec=15',text);self.assertIn('OnBootSec=10',text)
  service=(ROOT/'pi/systemd/bridge-agent.service').read_text()
  self.assertIn('Type=oneshot',service);self.assertIn('TimeoutStartSec=45',service)
 def test_hook_installed_and_never_restarts_media(self):
  text=(ROOT/'pi/scripts/bridge-fleet-network-up').read_text()
  self.assertIn('--no-block start bridge-agent.service',text)
  for bad in ('reboot','restart','bridge-gadget','bridge-feeder'):self.assertNotIn(bad,text)
  self.assertIn('/etc/NetworkManager/dispatcher.d/90-netbridge-fleet',(ROOT/'factory/ci-build-image.sh').read_text())
if __name__=='__main__':unittest.main()
