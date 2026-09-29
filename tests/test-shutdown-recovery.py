"""Fault injection for Stop, partial startup and immediate restart; no real media."""
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'app/netbridge-source'))
import source_app as a
POPEN = subprocess.Popen

class ShutdownRecovery(unittest.TestCase):
    def test_stop_closes_handles_and_cannot_be_revived_by_guard(self):
        s = a.Session()
        p = Mock(spec=POPEN, stdin=io.BytesIO())
        p.poll.return_value = 0
        log = io.StringIO()
        s.procs, s.logs, s.wanted = [p], [log], True
        s.voice_proc = p
        s.stop()
        self.assertFalse(s.wanted)
        self.assertFalse(s.live)
        self.assertTrue(log.closed)
        self.assertTrue(p.stdin.closed)
        self.assertIsNone(s.voice_proc)
        with patch.object(a, 'SESSION', s), patch.object(s, 'respawn_leg') as restart:
            a.StreamGuard()._tick()
            restart.assert_not_called()
        self.assertFalse(s.respawn_leg('video'))
        s.stop()  # idempotent

    def test_fast_new_session_gets_fresh_repair_budget(self):
        s = a.Session(); s.wanted = True; s.generation = 2
        guard = a.StreamGuard()
        guard.generation = (id(s), 1)
        guard.repairs = {'video': 5}; guard.giving_up = ['video']
        guard.live_since = 1
        with patch.object(a, 'SESSION', s), patch.object(s, 'respawn_leg') as repair:
            guard._tick()
            repair.assert_not_called()  # new startup grace period
        self.assertEqual(guard.repairs, {})
        self.assertEqual(guard.giving_up, [])
        self.assertEqual(guard.generation, (id(s), 2))

    def test_forced_media_exit_is_reaped(self):
        p = Mock(spec=POPEN, stdin=io.BytesIO())
        p.poll.return_value = None
        p.wait.side_effect = [subprocess.TimeoutExpired('helper', 0), subprocess.TimeoutExpired('helper', 0), 0]
        a._quit(p, timeout=0)
        p.kill.assert_called_once()
        self.assertEqual(p.wait.call_count, 3)

    def test_forced_mesh_exit_is_reaped(self):
        m = a.MeshManager()
        p = m.proc = Mock()
        p.wait.side_effect = [subprocess.TimeoutExpired('mesh', 0), 0]
        m.stop()
        p.kill.assert_called_once()
        self.assertEqual(p.wait.call_count, 2)
        self.assertIsNone(m.proc)

    def test_partial_start_failure_releases_camera_and_can_retry(self):
        s = a.Session(); s.return_on = False
        camera = Mock(spec=POPEN, stdin=io.BytesIO()); camera.poll.return_value = 0
        with tempfile.TemporaryDirectory() as tmp, patch.object(a, '_logdir', return_value=pathlib.Path(tmp)), patch.object(a, '_ffmpeg', return_value='fake'), patch.object(a, 'IS_MAC', False), patch.object(a, 'bind_video_ticket', side_effect=lambda args, ticket: args), patch.object(a.subprocess, 'Popen', side_effect=[camera, OSError('injected microphone launch failure')]):
            with self.assertRaisesRegex(RuntimeError, 'released'):
                s.start('127.0.0.1', 'camera', 'mic')
            self.assertFalse(s.wanted)
            self.assertEqual(s.procs, [])
            self.assertEqual(s.logs, [])
            self.assertTrue(camera.stdin.closed)
            with patch.object(a.subprocess, 'Popen', side_effect=lambda *args, **kw: Mock(spec=POPEN, stdin=io.BytesIO(), poll=Mock(return_value=0))):
                s.start('127.0.0.1', 'camera', 'mic')
                self.assertTrue(s.wanted)
                s.stop()

if __name__ == '__main__': unittest.main()
