#!/usr/bin/env python3
"""Exercise the desktop HTTP boundary without hardware or fleet access."""
import importlib.util
import json
import pathlib
import sys
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch, Mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'app/netbridge-source'))
import desktop_engine as desktop

class DesktopBoundary(unittest.TestCase):
    def setUp(self):
        for context in (patch.object(desktop, 'TOKEN', 'a'*36),
                        patch.object(desktop.engine, 'load_state', return_value={})):
            context.start(); self.addCleanup(context.stop)
        self.server = desktop.engine.ThreadingHTTPServer(('127.0.0.1', 0), desktop.DesktopHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:%s'%self.server.server_port
    def request(self, path, token=None, body=None, origin=None):
        headers={'Content-Type':'application/json'}
        if token: headers['X-NetBridge-Token']=token
        if origin: headers['Origin']=origin
        request=urllib.request.Request(self.base+path,headers=headers,data=json.dumps(body).encode() if body is not None else None)
        try: response=urllib.request.urlopen(request,timeout=3)
        except urllib.error.HTTPError as error: response=error
        with response: return response.status,json.loads(response.read())
    def test_reads_and_writes_require_per_launch_token(self):
        self.assertEqual(self.request('/api/state')[0],403)
        self.assertEqual(self.request('/api/state','wrong')[0],403)
        with patch.object(desktop.engine.SESSION,'stop') as stop:
            self.assertEqual(self.request('/api/stop',body={})[0],403)
            stop.assert_not_called()
        self.assertEqual(self.request('/api/state','a'*36)[0],200)
    def test_no_browser_ui_or_cross_origin_mutation(self):
        self.assertEqual(self.request('/','a'*36)[0],404)
        with patch.object(desktop.engine.SESSION,'stop') as stop:
            self.assertEqual(self.request('/api/signout','a'*36,{},'https://hostile.example')[0],403)
            stop.assert_not_called()
    def test_signout_stops_media(self):
        with patch.object(desktop.engine.SESSION,'stop') as stop, patch.object(desktop.engine.MESH,'stop'),patch.object(desktop.engine,'save_state'):
            self.assertEqual(self.request('/api/signout','a'*36,{})[0],200)
            stop.assert_called_once()
    def test_muted_badge_requires_capture_to_have_stopped(self):
        session=desktop.engine.Session()
        session.voice_muted=True
        session.voice_proc=Mock()
        session.voice_proc.poll.return_value=None
        with patch.object(desktop.engine,'SESSION',session):
            self.assertFalse(self.request('/api/state','a'*36)[1]['voice_muted'])
            session.voice_proc.poll.return_value=0
            self.assertTrue(self.request('/api/state','a'*36)[1]['voice_muted'])
    def test_desktop_status_has_no_account_token(self):
        status,value=self.request('/api/desktop/status','a'*36)
        self.assertEqual(status,200)
        self.assertEqual(set(value),{'wanted','bridge_host','desktop'})
if __name__=='__main__': unittest.main()
