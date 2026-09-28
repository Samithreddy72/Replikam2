import importlib.util,pathlib,unittest,json
ROOT=pathlib.Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('agent_results',ROOT/'pi/scripts/bridge-agent.py');a=importlib.util.module_from_spec(s);s.loader.exec_module(a)
class Results(unittest.TestCase):
 def test_diagnosis_larger_than_old_tail_survives(self):
  text=json.dumps({'findings':[{'detail':'x'*500} for _ in range(10)]})
  status,out=a.command_result('jitter-diagnose',0,text)
  self.assertEqual(status,'done');self.assertEqual(json.loads(out),json.loads(text))
 def test_invalid_and_oversize_diagnosis_never_succeed(self):
  for text in ('{"broken":',json.dumps({'detail':'x'*65536})):
   status,out=a.command_result('jitter-diagnose',0,text);self.assertEqual(status,'failed');self.assertIn('diagnostics bundle',out)
 def test_logs_remain_bounded_and_nonzero_result_is_failure(self):
  status,out=a.command_result('logs',1,'x'*9000);self.assertEqual(status,'failed');self.assertEqual(len(out),2000)
if __name__=='__main__':unittest.main()
