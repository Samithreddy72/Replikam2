"""Private engine for NetBridge Studio. The desktop shell owns this process.

No web UI, browser launch, or executable self-update in this mode. Authentication
is per launch, including reads. The legacy source app remains independently usable.
"""
import hmac
import json
import os
import pathlib
import socket
import sys
import threading
import urllib.parse

from process_guard import Lifecycle, guard_main

if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--netbridge-process-guard':
    guard_main(int(sys.argv[2]))
    raise SystemExit(0)

import source_app as engine

TOKEN = os.environ.get('NB_DESKTOP_TOKEN', '')


class DesktopHandler(engine.Handler):
    def authorized(self):
        if not TOKEN or not hmac.compare_digest(self.headers.get('X-NetBridge-Token', ''), TOKEN):
            self._send({'_error': 'Desktop authorization required'}, 403)
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        if self.path == '/api/desktop/status':
            return self._send({'wanted': engine.SESSION.wanted,
                               'bridge_host': engine.load_state().get('bridge_host'),
                               'desktop': True})
        if not self.path.startswith('/api/'):
            return self._send({'_error': 'Not found'}, 404)
        return super().do_GET()

    def do_POST(self):
        if not self.authorized():
            return
        if self.path == '/api/desktop/quit':
            if not self._csrf_ok():
                return self._send({'_error': 'Cross-origin request refused'}, 403)
            self._send({'ok': True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path == '/api/desktop/configure':
            if not self._csrf_ok():
                return self._send({'_error': 'Cross-origin request refused'}, 403)
            self._body_cache = None
            url = str(self._body().get('control_url', '')).rstrip('/')
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                return self._send({'_error': 'Use an HTTPS fleet URL without embedded credentials'}, 400)
            if engine.SESSION.wanted:
                return self._send({'_error': 'End the session before changing the fleet'}, 409)
            state = engine.load_state()
            if state.get('token') and state.get('control_url') != url:
                return self._send({'_error': 'Sign out before changing the fleet'}, 409)
            state['control_url'] = url
            engine.save_state(state)
            return self._send({'ok': True})
        # Shared handler owns sign-out cleanup for both Source and Studio.
        return super().do_POST()


def main():
    if len(TOKEN) < 32:
        raise SystemExit('A per-launch desktop token is required')
    # Prevent two desktop engines from sharing the fixed media ports. Hold this
    # advisory OS lock for the entire process, including shutdown. Crash releases it.
    if os.environ.get('NB_DESKTOP_STATE_DIR'):
        engine.STATE_DIR = pathlib.Path(os.environ['NB_DESKTOP_STATE_DIR'])
        engine.STATE_FILE = engine.STATE_DIR / 'state.json'
    engine.STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(engine.STATE_DIR / 'desktop.lock', 'a+b')
    try:
        if sys.platform == 'win32':
            import msvcrt
            lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit('NetBridge Studio is already running')
    # Never reap another app's live media. Ask users to close the legacy app first.
    with socket.socket() as probe:
        probe.settimeout(.3)
        if probe.connect_ex(('127.0.0.1', 8765)) == 0:
            raise SystemExit('Close the existing NetBridge Source app before opening Studio')
    engine.PORT = int(os.environ['NB_DESKTOP_PORT'])
    engine.Handler = DesktopHandler
    engine._open_browser = lambda url: None
    engine._app_binary = lambda: None  # whole desktop bundles must be updated together
    engine.apply_staged_update = lambda: None
    engine.check_for_update = lambda url: None
    engine._kill_orphan_media = lambda: None
    engine._kill_orphan_mesh = lambda **kwargs: None
    lifecycle = Lifecycle()
    lifecycle.start()
    lifecycle.watch_owner()
    try:
        engine.main()
    finally:
        lifecycle.finish()
        lock.close()


if __name__ == '__main__':
    main()
