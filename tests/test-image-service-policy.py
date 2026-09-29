import importlib.util
from pathlib import Path
import tempfile
import unittest
spec=importlib.util.spec_from_file_location('policy',Path(__file__).resolve().parents[1]/'factory/configure-service-policy.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Policy(unittest.TestCase):
    def test_snapshot_services_cannot_reactivate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);u=root/'etc/systemd/system';w=u/'multi-user.target.wants';w.mkdir(parents=True)
            for name in m.MASKED:
                (u/name).write_text('[Service]\nExecStart=/legacy\n');(w/name).symlink_to('../'+name)
            (u/'bridge-ssh.service').write_text('owner daemon')
            (w/'bridge-ssh.service').symlink_to('../bridge-ssh.service')
            m.configure(root);m.configure(root)
            for name in m.MASKED:
                self.assertEqual((u/name).readlink(),Path('/dev/null'))
                self.assertFalse((w/name).is_symlink())
            self.assertEqual((u/'bridge-ssh.service').read_text(),'owner daemon')
            self.assertTrue((w/'bridge-ssh.service').is_symlink())
    def test_directory_escape_refused(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root=Path(tmp);(root/'etc/systemd').mkdir(parents=True)
            (root/'etc/systemd/system').symlink_to(outside)
            with self.assertRaises(ValueError):m.configure(root)
if __name__=='__main__':unittest.main()
