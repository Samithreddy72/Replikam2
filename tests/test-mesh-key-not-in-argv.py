#!/usr/bin/env python3
"""The mesh auth key must reach the helper through its environment, never its argv.

argv is readable by every process on the machine (`ps -ax -o command`); on 2026-09-22 the
live presenter app was found starting netbridge-mesh with `--authkey tskey-auth-...` in plain
view. This runs the REAL MeshManager.route() launch code against a fake helper that records
what it was given, and checks the fake saw the key only in NB_MESH_AUTHKEY.

_kill_orphan_mesh is stubbed: the real one kills netbridge-mesh processes, which would cut a
live session on the machine running the tests.
"""
import json, os, pathlib, stat, sys, tempfile, unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'app/netbridge-source'))
import source_app as app

KEY = 'tskey-auth-TESTONLY-not-a-real-key-0000'

FAKE_HELPER = r'''#!/usr/bin/env python3
import json, os, sys
open(os.environ["FAKE_RECORD"], "w").write(json.dumps({
    "argv": sys.argv, "env_key": os.environ.get("NB_MESH_AUTHKEY")}))
print(json.dumps({"ready": True, "tailnet_ip": "100.64.0.9", "control_port": 18099}), flush=True)
'''


class MeshKeyNotInArgv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='meshkey-')
        self.helper = os.path.join(self.tmp, 'netbridge-mesh')
        with open(self.helper, 'w') as f:
            f.write(FAKE_HELPER)
        os.chmod(self.helper, os.stat(self.helper).st_mode | stat.S_IEXEC)
        self.record = os.path.join(self.tmp, 'record.json')
        os.environ['FAKE_RECORD'] = self.record

    def tearDown(self):
        os.environ.pop('FAKE_RECORD', None)

    def launch(self):
        mm = app.MeshManager()
        with patch.object(app, '_mesh_bin', return_value=self.helper), \
             patch.object(app, '_kill_orphan_mesh', return_value=None), \
             patch.object(app, '_logdir', return_value=pathlib.Path(self.tmp)), \
             patch.object(app, '_mesh_hostname', return_value='netbridge-source-test'), \
             patch.object(app.MeshManager, '_mint_key', return_value=(KEY, '')):
            route = mm.route({'id': 'dev1', 'tailscale_ip': '100.64.0.1', 'ip': '192.0.2.1'},
                             {'token': 't', 'control_url': 'https://example.invalid'})
        if mm.proc:
            mm.proc.wait(timeout=10)
        return route, json.load(open(self.record))

    def test_key_is_not_on_the_command_line(self):
        route, seen = self.launch()
        self.assertEqual(route.get('via'), 'mesh', route)
        self.assertNotIn(KEY, ' '.join(seen['argv']))
        self.assertNotIn('--authkey', seen['argv'])

    def test_key_arrives_in_the_environment(self):
        _, seen = self.launch()
        self.assertEqual(seen['env_key'], KEY)

    def test_key_does_not_leak_into_the_apps_own_environment(self):
        self.launch()
        self.assertNotIn('NB_MESH_AUTHKEY', os.environ)


if __name__ == '__main__':
    unittest.main()
