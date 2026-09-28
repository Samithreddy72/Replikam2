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
if __name__=='__main__':unittest.main()
