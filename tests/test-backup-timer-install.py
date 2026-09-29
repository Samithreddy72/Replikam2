"""Install over a streamed SSH-style stdin; Docker must not consume the remaining script."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
class Timer(unittest.TestCase):
    def test_streamed_install_completes_after_database_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); bin=root/'bin';bin.mkdir();units=root/'units';log=root/'calls'
            for name,body in {'id':'echo 0', 'docker':'cat >/dev/null\necho verified-backup',
                              'systemctl':'echo "$@" >> "$TEST_CALLS"'}.items():
                script=bin/name;script.write_text('#!/bin/sh\n'+body+'\n');script.chmod(0o755)
            source=(ROOT/'control-plane/deploy/aws/install-backup-timer.sh').read_text()
            source=source.replace('/opt/netbridge',str(root)).replace('/etc/systemd/system',str(units))
            subprocess.run(['bash','-s'],input=source,text=True,check=True,
                env=dict(os.environ,PATH=str(bin)+os.pathsep+os.environ['PATH'],TEST_CALLS=str(log)),
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
            self.assertIn('OnCalendar=daily',(units/'netbridge-backup.timer').read_text())
            self.assertIn('enable --now netbridge-backup.timer',log.read_text())
            self.assertTrue((units/'netbridge-backup.service').exists())
if __name__=='__main__':unittest.main()
