"""The release test gate must fail despite misleading success/skip output."""
import pathlib,tempfile,subprocess,unittest,shutil
RUNNER=pathlib.Path(__file__).resolve().parents[1]/'tools/run-tests.sh'
class Gate(unittest.TestCase):
    def run_case(self, script):
        with tempfile.TemporaryDirectory() as td:
            root=pathlib.Path(td);(root/'tools').mkdir();(root/'tests').mkdir()
            shutil.copy(RUNNER,root/'tools/run-tests.sh')
            (root/'tests/test-fixture.sh').write_text(script)
            return subprocess.run(['bash',str(root/'tools/run-tests.sh')],capture_output=True,text=True)
    def test_nonzero_after_success_footer_fails_gate(self):
        r=self.run_case("echo '3 passed, 0 failed'\nexit 7\n")
        self.assertNotEqual(r.returncode,0,r.stdout);self.assertIn('NOT GREEN',r.stdout)
    def test_skip_then_crash_cannot_hide_failure(self):
        r=self.run_case("echo 'SKIPPED missing optional thing'\nexit 2\n")
        self.assertNotEqual(r.returncode,0,r.stdout)
    def test_unittest_skip_is_reported_and_blocks_release(self):
        r=self.run_case("echo 'Ran 3 tests in 0.001s'\necho 'OK (skipped=1)'\n")
        self.assertNotEqual(r.returncode,0,r.stdout);self.assertIn('2 passed, 0 failed, 1 skipped',r.stdout)
    def test_success_passes(self):
        r=self.run_case("echo '3 passed, 0 failed'\n")
        self.assertEqual(r.returncode,0,r.stdout)
if __name__=='__main__':unittest.main()
