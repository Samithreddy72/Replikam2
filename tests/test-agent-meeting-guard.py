import importlib.util,pathlib,unittest
from unittest.mock import patch
from types import SimpleNamespace
p=pathlib.Path(__file__).resolve().parents[1]/'pi/scripts/bridge-agent.py'
s=importlib.util.spec_from_file_location('guard_agent',p);a=importlib.util.module_from_spec(s);s.loader.exec_module(a)
class Guard(unittest.TestCase):
 def test_device_rechecks_before_any_process_launch(self):
  for status in ({},{'udc':'configured','streams':{}},{'udc':'not attached','streams':{}},{'udc':'not attached','streams':{'video':False,'voice':None}},{'udc':'not attached','streams':{'voice':True}}):
   with patch.object(a,'telemetry',return_value=status),patch.object(a.subprocess,'run') as run:
    self.assertEqual(a.run_command({'id':'1','type':'reboot'})[1],'rejected');run.assert_not_called()
 def test_idle_and_explicit_override_are_distinct(self):
  for status,safety in [({'udc':'not attached','streams':{'video':False,'voice':False}},{}),({'udc':'configured','streams':{}},{'force_live':True})]:
   with patch.object(a,'telemetry',return_value=status),patch.object(a,'DETACHED',{}),patch.object(a.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')) as run:
    self.assertEqual(a.run_command({'id':'1','type':'reboot','safety':safety})[1],'done');run.assert_called_once()
if __name__=='__main__':unittest.main()
