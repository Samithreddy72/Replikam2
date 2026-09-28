"""CLI target ambiguity must never turn into an action against another bridge."""
import contextlib,io,importlib.machinery,importlib.util,pathlib,tempfile,unittest
from unittest.mock import patch
path=pathlib.Path(__file__).resolve().parents[1]/'tools/nb'
spec=importlib.util.spec_from_loader('nb_safety',importlib.machinery.SourceFileLoader('nb_safety',str(path)))
nb=importlib.util.module_from_spec(spec);spec.loader.exec_module(nb)
class Safety(unittest.TestCase):
 def test_missing_fleet_number_cannot_fall_back_to_id_or_name(self):
  with patch.object(nb,'devices',return_value=[{'id':'device003','number':2,'name':'3 room'}]),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
   for value in ('3','NB-003','nb3','0003','999999'):
    with self.assertRaises(SystemExit):nb.pick(value)
   self.assertEqual(nb.pick('2')[0]['id'],'device003')
 def test_duplicate_name_is_ambiguous(self):
  with patch.object(nb,'devices',return_value=[{'id':'a','name':'Room'},{'id':'b','name':'Room'}]),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
   with self.assertRaises(SystemExit):nb.pick('Room')
 def test_weeks_are_included(self):
  self.assertEqual(nb.short_uptime('2 weeks, 3 days, 4 hours, 5 minutes'),'17d04h05m')
 def test_transport_failure_never_blindly_retries_a_write(self):
  with tempfile.TemporaryDirectory() as td:
   token=pathlib.Path(td)/'token';token.write_text('test')
   for error in (TimeoutError(),nb.http.client.IncompleteRead(b'partial'),ConnectionResetError()):
    with patch.object(nb,'TOKEN_FILE',token),patch.object(nb.urllib.request,'urlopen',side_effect=error) as call,contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
     with self.assertRaises(SystemExit):nb.api('POST','/admin/devices/a/commands',{'type':'reboot'})
     call.assert_called_once()
 def test_interrupted_http_error_body_is_reported_without_traceback(self):
  with tempfile.TemporaryDirectory() as td:
   token=pathlib.Path(td)/'token';token.write_text('test')
   error=nb.urllib.error.HTTPError('https://fleet.invalid',403,'forbidden',{},None)
   with patch.object(error,'read',side_effect=TimeoutError()),patch.object(nb,'TOKEN_FILE',token),patch.object(nb.urllib.request,'urlopen',side_effect=error),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    with self.assertRaises(SystemExit):nb.api('POST','/admin/devices/a/commands',{})
 def test_malformed_telemetry_cannot_break_inventory(self):
  bad={'id':'abc','name':'Room','number':1,'online':True,'latest':{'pin':{'session':42,'protocol':'oops'},'streams':[], 'power':{'rate':[]},'overrides':{'pending':[{},3],'active':4},'quarantined':[{},2],'usb':42,'clock':True}}
  with patch.object(nb,'devices',return_value=[bad]),patch.object(nb,'api',return_value=bad),contextlib.redirect_stdout(io.StringIO()):
   nb.cmd_list([]);nb.cmd_show(['1'])
 def test_offline_status_is_never_presented_as_current_live(self):
  device={'id':'abc','name':'Room','number':1,'online':False,'latest':{'streams':{'video':True},'pin':{'session':{'active':True}},'udc':'configured'}}
  self.assertEqual(nb.live(device),(None,None))
  out=io.StringIO()
  with patch.object(nb,'devices',return_value=[device]),patch.object(nb,'api',return_value=device),contextlib.redirect_stdout(out):
   nb.cmd_list([]);nb.cmd_show(['1'])
  self.assertIn('?/?/?',out.getvalue());self.assertIn('UNKNOWN while offline',out.getvalue())
 def test_legacy_pin_warning_precedes_confirmation_and_queueing(self):
  out=io.StringIO();device={'id':'abc','name':'Room','latest':{'pin':{'protocol':1}}}
  def confirm(*args):self.assertIn('write the PIN to its device log',out.getvalue())
  def api(method,*args,**kwargs):
   self.assertIn('write the PIN to its device log',out.getvalue())
   return {'pin':'1234','command_id':'c'} if method=='POST' else [{'id':'c','status':'done','output':'done'}]
  with patch.object(nb,'pick',return_value=[device]),patch.object(nb,'confirm',side_effect=confirm),patch.object(nb,'api',side_effect=api),patch.object(nb.time,'sleep'),contextlib.redirect_stdout(out):
   nb.cmd_pin(['1','1234'])
if __name__=='__main__':unittest.main()
