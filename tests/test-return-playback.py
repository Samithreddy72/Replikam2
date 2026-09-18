#!/usr/bin/env python3
"""Exercise return launch settings and stderr retention without an audio device."""
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import time
import types
from unittest.mock import patch, Mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "source_app", ROOT / "app/netbridge-source/source_app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class ReturnPlayback(unittest.TestCase):
    def test_system_default_change_restarts_only_voice(self):
        session = Mock(wanted=True, live=True, voice_backend='gstreamer-coreaudio',
                       voice_device_id=42,
                       voice_capture=('System default microphone', '127.0.0.1', 0))
        session.leg_status.return_value = {'video': True, 'voice': True, 'return': True}
        session.respawn_leg.return_value = True
        guard = app.StreamGuard()
        guard.live_since = time.time() - 60
        resolve = Mock(return_value=42)
        with patch.object(app, 'SESSION', session), patch.dict(sys.modules, {
                'audio_engine': types.SimpleNamespace(mac_microphone_device_id=resolve)}):
            guard._tick()
            session.suspend_voice.assert_not_called()
            resolve.return_value = 113  # macOS falls back after USB disconnect.
            guard._tick()
        resolve.assert_called_with('System default microphone')
        session.suspend_voice.assert_called_once()
        session.respawn_leg.assert_called_once_with('voice')
        self.assertFalse(guard.snapshot()['waiting_for_mic'])

    def test_quick_replug_restarts_alive_capture_with_changed_device(self):
        session = Mock(wanted=True, live=True, voice_backend='gstreamer-coreaudio',
                       voice_device_id=42, voice_capture=('Selected USB mic', '127.0.0.1', 0))
        session.leg_status.return_value = {'video': True, 'voice': True, 'return': True}
        session.respawn_leg.return_value = True
        guard = app.StreamGuard()
        guard.live_since = time.time() - 60
        with patch.object(app, 'SESSION', session), patch.dict(sys.modules, {
                'audio_engine': types.SimpleNamespace(mac_microphone_device_id=lambda name: 43)}):
            guard._tick()
        session.suspend_voice.assert_called_once()
        session.respawn_leg.assert_called_once_with('voice')

    def test_unplug_waits_and_reconnect_resumes_without_retry_exhaustion(self):
        session = Mock()
        session.wanted = session.live = True
        session.voice_backend = 'gstreamer-coreaudio'
        session.voice_capture = ('Selected USB mic', '127.0.0.1', 0)
        session.voice_device_id = 42
        session.leg_status.return_value = {'video': True, 'voice': False, 'return': True}
        session.respawn_leg.return_value = True
        guard = app.StreamGuard()
        guard.live_since = time.time() - 60
        resolve = Mock(side_effect=RuntimeError('not attached'))
        with patch.object(app, 'SESSION', session), patch.dict(sys.modules, {
                'audio_engine': types.SimpleNamespace(mac_microphone_device_id=resolve)}):
            for _ in range(20):
                guard._tick()
            session.respawn_leg.assert_not_called()
            self.assertTrue(guard.snapshot()['waiting_for_mic'])
            self.assertEqual(guard.snapshot()['repairs'], {})
            resolve.side_effect = None
            resolve.return_value = 42
            guard._tick()
            session.respawn_leg.assert_called_once_with('voice')
            self.assertFalse(guard.snapshot()['waiting_for_mic'])
            session.wanted = False
            session.respawn_leg.reset_mock()
            guard._tick()
            session.respawn_leg.assert_not_called()

    def test_microphone_retry_refreshes_device_identity(self):
        session = app.Session()
        session.wanted = True
        session.voice_backend = 'gstreamer-coreaudio'
        session.voice_capture = ('Selected USB mic', '127.0.0.1', 0)
        session.voice_device_id = 42
        session.leg_argv = {'voice': ['test-gst', 'unique-id=disconnected-device']}
        session.leg_proc = {}
        session.leg_env = {'voice': {}}
        fresh = ['test-gst', '-e', 'osxaudiosrc', 'device=42']
        resolve = Mock(return_value=fresh)
        child = Mock()
        child.poll.return_value = None
        with patch.dict(sys.modules, {'audio_engine': types.SimpleNamespace(
                mac_microphone_argv=resolve, mac_microphone_device_id=Mock(return_value=42))}), patch.object(
                    app.subprocess, 'Popen', return_value=child) as popen:
            self.assertTrue(session.respawn_leg('voice'))
        resolve.assert_called_once_with('test-gst', 'Selected USB mic', '127.0.0.1', app.RTP_VOICE, 0)
        self.assertEqual(popen.call_args.args[0], fresh)
        self.assertEqual(session.leg_argv['voice'], fresh)
        for handle in session.logs:
            handle.close()

    def test_default_retry_tracks_parent_identity_separately_from_capture(self):
        session = app.Session()
        session.wanted = True
        session.voice_backend = 'gstreamer-coreaudio'
        session.voice_capture = ('System default microphone', '127.0.0.1', 0)
        session.voice_device_id = 42
        session.leg_argv = {'voice': ['test-gst', 'unique-id=disconnected-device']}
        session.leg_proc = {}
        session.leg_env = {'voice': {}}
        fresh = ['test-gst', '-e', 'osxaudiosrc', 'device=0']
        resolve = Mock(return_value=fresh)
        child = Mock()
        child.poll.return_value = None
        with patch.dict(sys.modules, {'audio_engine': types.SimpleNamespace(
                mac_microphone_argv=resolve, mac_microphone_device_id=Mock(return_value=42))}), patch.object(
                    app.subprocess, 'Popen', return_value=child) as popen:
            self.assertTrue(session.respawn_leg('voice'))
        resolve.assert_called_once_with('test-gst', 'System default microphone', '127.0.0.1', app.RTP_VOICE, 0)
        self.assertEqual(popen.call_args.args[0], fresh)
        self.assertEqual(session.leg_argv['voice'], fresh)
        self.assertEqual(session.voice_device_id, 42)
        for handle in session.logs:
            handle.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for context in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(app, "IS_MAC", True),
            patch.object(app, "IS_WIN", False),
            patch.object(app, "_gst", return_value="test-gst"),
            patch.object(app, "_gst_env", return_value={}),
            patch.object(app, "_logdir", return_value=pathlib.Path(self.tmp.name)),
        ):
            context.start()
            self.addCleanup(context.stop)

    def launch(self, session):
        captured = {}
        real_popen = subprocess.Popen

        def child(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs["env"]
            # Real child writes after inheriting the log FD; the parent's context
            # manager closes its own handle before we wait for the child.
            return real_popen([sys.executable, "-c",
                               "import sys; sys.stderr.write('test sink warning\\n')"],
                              **kwargs)

        with patch.object(app.subprocess, "Popen", side_effect=child):
            self.assertEqual(session._start_return(5004), "gstreamer")
        self.assertEqual(session.return_proc.wait(timeout=5), 0)
        return captured

    def test_mac_launch_and_previous_log(self):
        session = app.Session()
        result = self.launch(session)
        self.assertIn("sync=true", result["argv"])
        self.assertIn("volume=1.0", result["argv"])
        self.assertNotIn("audiodynamic", result["argv"])
        self.assertIn("use-inband-fec=true", result["argv"])
        self.assertNotIn("plc=true", result["argv"])
        self.assertIn("latency=250", result["argv"])
        self.assertEqual(result["env"]["GST_DEBUG"], "2")
        log = pathlib.Path(self.tmp.name) / "netbridge-source-return.log"
        first = log.read_text()
        self.assertIn("test sink warning", first)
        self.assertIn("dynamics=False", first)
        session.return_gain = "1.5"
        self.launch(session)
        self.assertEqual(log.with_suffix(".log.previous").read_text(), first)
        self.assertIn("gain=1.5", log.read_text())
        self.assertEqual(session.logs, [])  # no parent file handles accumulate

    def test_legacy_environment_overrides(self):
        with patch.dict(os.environ, {"NB_RETURN_GAIN": "2.0",
                                    "NB_RETURN_DYNAMICS": "1",
                                    "NB_RETURN_SINK_SYNC": "0"}):
            result = self.launch(app.Session())
        self.assertIn("volume=2.0", result["argv"])
        self.assertIn("audiodynamic", result["argv"])
        self.assertIn("sync=false", result["argv"])

    def test_windows_keeps_existing_recipe(self):
        with patch.object(app, "IS_MAC", False), patch.object(app, "IS_WIN", True):
            result = self.launch(app.Session())
        self.assertIn("autoaudiosink", result["argv"])
        self.assertIn("volume=2.0", result["argv"])
        self.assertIn("audiodynamic", result["argv"])
        self.assertIn("sync=false", result["argv"])

    def test_repeated_tuning_does_not_restart_audio(self):
        session = app.Session()
        with patch.object(session, "set_return") as restart:
            session.set_return_tuning(gain=1, jitter_ms=250, dynamics=False, sink_sync=True)
            session.set_return_tuning(gain="1.00", jitter_ms="250")
            restart.assert_not_called()
            session.set_return_tuning(jitter_ms=400)
            self.assertEqual([c.args for c in restart.call_args_list], [(False,), (True,)])
            restart.reset_mock()
            session.set_return_tuning(jitter_ms=400)
            restart.assert_not_called()

    def test_zero_buffer_trial_is_not_clamped_to_sixty(self):
        session = app.Session()
        with patch.object(session, "set_return"):
            self.assertEqual(session.set_return_tuning(jitter_ms=0)["jitter_ms"], "0")
            self.assertEqual(session.set_return_tuning(jitter_ms=-1)["jitter_ms"], "0")
            self.assertEqual(session.set_return_tuning(jitter_ms=1001)["jitter_ms"], "1000")

    def test_watchdog_ignores_intentionally_disabled_return(self):
        session = types.SimpleNamespace(live=True, return_on=False, return_port=5004)
        helper = types.SimpleNamespace(proc=types.SimpleNamespace(poll=lambda: None))
        watch = app.LegWatch()
        watch.live_since = time.time() - 60
        with patch.object(app, "SESSION", session), patch.object(app, "MESH", helper), \
                patch.object(app, "_leg_bound", side_effect=lambda p: p != 5004) as bound:
            watch._tick()
            watch._tick()
            self.assertEqual(watch.snapshot()["missing"], [])
            self.assertNotIn(5004, [c.args[0] for c in bound.call_args_list])
            session.return_on = True
            watch._tick()
            watch._tick()
            self.assertEqual(watch.snapshot()["missing"], [5004])
            session.return_on = False
            watch._tick()
            self.assertEqual(watch.snapshot()["missing"], [])

    def test_source_media_directory(self):
        root = pathlib.Path(self.tmp.name)
        (root / "gst/plugins").mkdir(parents=True)
        for name in ("ffmpeg", "gst/gst-launch-1.0", "netbridge-mesh"):
            p = root / name
            p.touch()
            p.chmod(0o755)
        # Undo the launch stubs so we exercise actual dependency discovery.
        with patch.dict(os.environ, {"NB_MEDIA_DIR": str(root)}):
            self.assertEqual(app._ffmpeg(), str(root / "ffmpeg"))
            self.assertEqual(app._mesh_bin(), str(root / "netbridge-mesh"))


if __name__ == "__main__":
    unittest.main()
