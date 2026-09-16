#!/usr/bin/env python3
"""A failed tuning write must not report success or restart working audio."""
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class TuningWrite(unittest.TestCase):
    def run_tune(self, failure):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            sudo = root / "sudo"
            sudo.write_text('''#!/bin/bash
case "$1" in
 tee)
   cat > "$TEST_DIR/requested"
   if [ "$TEST_FAILURE" = write ]; then
     echo 'tee: Read-only file system' >&2; exit 1
   fi
   cp "$TEST_DIR/requested" "$TEST_DIR/applied" ;;
 systemctl)
   echo "$*" >> "$TEST_DIR/restarts"
   [ "$TEST_FAILURE" != restart ] ;;
 *) exit 99 ;;
esac
''')
            sudo.chmod(0o755)
            sleep = root / "sleep"
            sleep.write_text("#!/bin/sh\nexit 0\n")
            sleep.chmod(0o755)
            env = dict(os.environ, PATH=td+os.pathsep+os.environ["PATH"],
                       TEST_DIR=td, TEST_FAILURE=failure)
            result = subprocess.run(["bash", str(ROOT / "pi/scripts/bridge"),
                                     "return-tune", "provide-clock=false", "identity"],
                                    env=env, capture_output=True, text=True, timeout=5)
            return result, (root / "restarts").exists(), (root / "applied").exists()

    def test_readonly_write_does_not_restart(self):
        result, restarted, applied = self.run_tune("write")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(restarted)
        self.assertFalse(applied)
        self.assertIn("NOT applied", result.stderr)

    def test_restart_failure_is_not_success(self):
        result, restarted, applied = self.run_tune("restart")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(restarted)
        self.assertTrue(applied)

    def test_successful_write_restarts(self):
        result, restarted, applied = self.run_tune("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(restarted)
        self.assertTrue(applied)


if __name__ == "__main__":
    unittest.main()
