#!/usr/bin/env python3
"""HTTP contracts for diagnostics without starting cameras, mesh or audio devices."""
import importlib.util
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch, Mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_app', ROOT/'app/netbridge-source/source_app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

class AudioAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = app.Session()
        for context in (patch.object(app, 'SESSION', self.session),
                        patch.object(app, 'load_state', return_value={}),
                        patch.object(app, 'save_state'),
                        patch.object(app, '_logdir', return_value=pathlib.Path(self.tmp.name))):
            context.start(); self.addCleanup(context.stop)
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:%d'%self.server.server_port

    def request(self, path, body=None, origin=None):
        headers = {'Content-Type':'application/json'}
        if origin: headers['Origin'] = origin
        request = urllib.request.Request(self.url+path,
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try: response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as e: response = e
        with response: return response.status, response.read()

    def test_idle_and_cross_origin_refuse_mutations(self):
        self.assertFalse(json.loads(self.request('/api/audio/diagnostics')[1])['running'])
        self.assertEqual(self.request('/api/audio/capture', {})[0],409)
        self.assertEqual(self.request('/api/audio/recover', {})[0],409)
        self.assertEqual(self.request('/api/audio/recover', {}, 'https://example.com')[0],403)

    def test_capture_duration_and_fixed_file_download(self):
        player = Mock()
        player.start_capture.side_effect = ValueError('capture duration must be 1–60 seconds')
        self.session.return_proc = player
        self.assertEqual(self.request('/api/audio/capture', {'seconds':61})[0],400)
        ident = 'a'*32
        directory = pathlib.Path(self.tmp.name)/'captures'/ident
        directory.mkdir(parents=True)
        (directory/'decoded.wav').write_bytes(b'test audio')
        self.assertEqual(self.request('/api/audio/captures/'+ident+'/decoded.wav'), (200,b'test audio'))
        self.assertEqual(self.request('/api/audio/captures/'+ident+'/other.wav')[0],404)

    def test_manual_buffer_survives_fleet_and_can_be_released(self):
        code, data = self.request('/api/return-tuning', {'jitter_ms':0})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(data)['manual_jitter'])
        app.save_state.assert_called_with({'return_manual_jitter_ms':0})
        watcher = app.BridgeWatch()
        watcher._apply_fleet_tuning({'ts':1, 'jitter_ms':600})
        self.assertEqual(self.session.return_jitter_ms, '0')
        self.assertEqual(self.request('/api/return-tuning', {'jitter_ms':'bad'})[0],400)
        self.request('/api/return-tuning', {'auto_jitter':True})
        watcher._apply_fleet_tuning({'ts':2, 'jitter_ms':600})
        self.assertEqual(self.session.return_jitter_ms, '600')

    def test_failed_recovery_and_cooldown(self):
        self.session.wanted = True
        self.session.return_on = True
        with patch.object(self.session, 'set_return') as restart:
            self.assertEqual(self.request('/api/audio/recover', {})[0],503)
            self.assertEqual([c.args for c in restart.call_args_list],[(False,),(True,)])
            self.assertEqual(self.request('/api/audio/recover', {})[0],429)

if __name__ == '__main__': unittest.main()
