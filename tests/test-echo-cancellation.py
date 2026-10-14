#!/usr/bin/env python3
"""Echo cancellation moves the microphone in-process, binds it to the live return player's
probe, and never costs the presenter their voice when anything about it is unavailable."""
import itertools
import json
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'app/netbridge-source'))
import audio_engine
import source_app as app

PROBES = itertools.count(1)


class FakeEngine:
    """Process-like surface shared by AudioEngine and MicEngine."""
    in_process = True
    stdin = None
    pid = None

    def __init__(self):
        self.rc = None

    def poll(self):
        return self.rc

    def terminate(self):
        self.rc = 0 if self.rc is None else self.rc

    kill = terminate

    def wait(self, timeout=None):
        return self.rc


class FakeReturn(FakeEngine):
    def __init__(self, order):
        super().__init__()
        self.echo_probe = 'probe-%d' % next(PROBES)   # a new player brings a new probe
        self.order = order

    def attach_microphone(self, name, host, port, gain_db=0, source=None):
        self.order.append('attach')
        return FakeMic(name, host, port, gain_db, echo_probe=self.echo_probe, player=self)

    def terminate(self):
        super().terminate()
        for mic in FakeMic.started:          # an attached microphone stops with its player
            if mic.player is self and mic.rc is None:
                mic.rc = 1


class FakeMic(FakeEngine):
    started = []

    def __init__(self, name, host, port, gain_db=0, echo_probe=None, source=None, player=None):
        super().__init__()
        self.name, self.host, self.port, self.echo_probe = name, host, port, echo_probe
        self.player = player
        FakeMic.started.append(self)


class EchoSession(unittest.TestCase):
    def setUp(self):
        FakeMic.started = []
        self.spawned, self.order = [], []
        self.session = app.Session()
        self.session.echo_cancel = True

        def spawn(argv, **kw):
            p = Mock()
            p.poll.return_value = None
            p.argv = argv
            self.spawned.append(p)
            return p

        def start_return(session, port):
            self.order.append('return')
            session.return_proc = FakeReturn(self.order)
            session.procs.append(session.return_proc)
            return 'gstreamer-persistent'

        def mic(*args, **kw):              # the standalone MicEngine: never a canceller
            self.order.append('standalone')
            return FakeMic(*args, **kw)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for context in (patch.object(app, '_ffmpeg', return_value='ffmpeg'),
                        patch.object(app, '_gst', return_value=None),
                        patch.object(app, 'IS_WIN', True), patch.object(app, 'IS_MAC', False),
                        patch.object(app.subprocess, 'Popen', side_effect=spawn),
                        patch.object(app, '_logdir', return_value=pathlib.Path(self.tmp.name)),
                        patch.object(app.PINS, 'ticket', return_value='test-ticket'),
                        patch.object(app, '_echo_cancellation_available', return_value=True),
                        patch.object(app, 'av_devices', return_value={'audio': [{'name': 'Test mic', 'index': 'Test mic'}]}),
                        patch.object(app.Session, '_start_return', start_return),
                        patch.object(audio_engine, 'MicEngine', side_effect=mic)):
            context.start()
            self.addCleanup(context.stop)
        self.addCleanup(self.session.stop)

    def start(self, **kw):
        self.session.start('127.0.0.1', 'Test camera', 'Test mic', mic_name='Test mic', **kw)
        self.session.capture_names = {'video': 'Test camera', 'voice': 'Test mic'}

    def subprocess_voice(self):
        return [p for p in self.spawned if 'audio=Test mic' in p.argv]

    def test_microphone_starts_in_process_after_return_with_its_probe(self):
        self.start()
        mic = self.session.voice_proc
        self.assertIsInstance(mic, FakeMic)
        self.assertEqual(self.order, ['return', 'attach'])   # inside the player's pipeline
        self.assertIs(mic.player, self.session.return_proc)
        self.assertEqual(mic.echo_probe, self.session.return_proc.echo_probe)
        self.assertEqual((mic.host, mic.port), ('127.0.0.1', app.RTP_VOICE))
        self.assertEqual(self.subprocess_voice(), [])     # ffmpeg never opened the mic
        self.assertIn(mic, self.session.procs)
        self.assertEqual(self.session.voice_backend, 'gstreamer-inprocess')
        self.assertTrue(self.session.echo_status()['active'])
        self.assertTrue(self.session.leg_status()['voice'])

    def test_inprocess_failure_falls_back_to_the_standard_microphone(self):
        with patch.object(FakeReturn, 'attach_microphone', side_effect=RuntimeError('No echo probe')):
            self.start()
        self.assertEqual(len(self.subprocess_voice()), 1)
        self.assertEqual(self.session.voice_backend, 'ffmpeg')
        status = self.session.echo_status()
        self.assertFalse(status['active'])
        self.assertIn('No echo probe', status['note'])

    def test_meeting_audio_off_and_on_keeps_the_presenter_audible(self):
        self.start()
        first_mic, first_player = self.session.voice_proc, self.session.return_proc
        # The attached microphone stops with its player; the presenter must stay audible.
        self.session.set_return(False)
        alone = self.session.voice_proc
        self.assertIsNot(alone, first_mic)
        self.assertNotEqual(first_mic.poll(), None)
        self.assertNotIn(first_mic, self.session.procs)
        self.assertIsNone(alone.echo_probe)
        self.assertIsNone(alone.poll())
        self.assertIn('not playing', self.session.echo_status()['note'])
        # Meeting audio back: the microphone moves into the new player, against its new probe.
        self.session.set_return(True)
        mic = self.session.voice_proc
        self.assertIsNot(mic, alone)
        self.assertEqual(alone.poll(), 0)
        self.assertIsNot(self.session.return_proc, first_player)
        self.assertIs(mic.player, self.session.return_proc)
        self.assertNotEqual(mic.echo_probe, first_mic.echo_probe)
        self.assertTrue(self.session.echo_status()['active'])

    def test_no_meeting_audio_here_means_no_canceller(self):
        self.session.return_on = False
        self.start()
        self.assertIsNone(self.session.voice_proc.echo_probe)
        self.assertIn('not playing', self.session.echo_status()['note'])

    def test_switching_off_and_on_restarts_only_the_microphone(self):
        self.start()
        video, player = self.session.leg_proc['video'], self.session.return_proc
        self.assertFalse(self.session.set_echo_cancel(False)['active'])
        self.assertIsNone(self.session.voice_proc.echo_probe)
        self.assertTrue(self.session.set_echo_cancel(True)['active'])
        self.assertEqual(self.session.voice_proc.echo_probe, player.echo_probe)
        self.assertIs(self.session.leg_proc['video'], video)
        self.assertIs(self.session.return_proc, player)
        self.assertEqual(len(FakeMic.started), 3)

    def test_switching_on_mid_session_moves_the_microphone_in_process(self):
        self.session.echo_cancel = False
        self.start()
        self.assertEqual(len(self.subprocess_voice()), 1)
        ffmpeg = self.session.voice_proc
        self.assertTrue(self.session.set_echo_cancel(True)['active'])
        self.assertIsInstance(self.session.voice_proc, FakeMic)
        self.assertNotIn(ffmpeg, self.session.procs)

    def test_unavailable_keeps_the_existing_microphone(self):
        with patch.object(app, '_echo_cancellation_available', return_value=False):
            self.start()
            self.assertEqual(self.session.voice_backend, 'ffmpeg')
            self.session.set_echo_cancel(True)
        self.assertEqual(len(self.subprocess_voice()), 1)
        self.assertEqual(FakeMic.started, [])

    def test_muted_start_then_unmute_uses_the_canceller(self):
        self.start(voice_muted=True)
        self.assertEqual(FakeMic.started, [])
        self.assertFalse(self.session.set_voice_muted(False))
        self.assertEqual(self.session.voice_proc.echo_probe, self.session.return_proc.echo_probe)
        self.assertTrue(self.session.set_voice_muted(True))
        self.assertEqual(FakeMic.started[0].poll(), 0)    # muting releases the device

    def test_unplugged_microphone_waits_instead_of_opening_another(self):
        self.start()
        with patch.object(app, 'av_devices', return_value={'audio': []}):
            self.assertFalse(self.session.respawn_leg('voice'))
        self.assertEqual(self.session.waiting_for_devices.get('voice'), 'Test mic')
        self.assertEqual(len(FakeMic.started), 1)

    def test_stop_releases_the_inprocess_microphone(self):
        self.start()
        mic = self.session.voice_proc
        self.session.stop()
        self.assertIsNotNone(mic.poll())     # ended with its player, or terminated itself

    def test_mac_inprocess_microphone_is_followed_across_usb_changes(self):
        with patch.object(app, 'IS_WIN', False), patch.object(app, 'IS_MAC', True), \
                patch.object(audio_engine, 'mac_microphone_device_id', return_value=42):
            self.start()
            self.assertEqual(self.session.voice_device_id, 42)
            self.assertTrue(app._voice_follows_coreaudio(self.session))
        self.assertFalse(app._voice_follows_coreaudio(self.session))   # Windows: no CoreAudio


class EchoAPI(unittest.TestCase):
    def setUp(self):
        self.session = app.Session()
        self.saved = []
        for context in (patch.object(app, 'SESSION', self.session),
                        patch.object(app, 'load_state', return_value={}),
                        patch.object(app, 'save_state', side_effect=lambda st: self.saved.append(dict(st))),
                        patch.object(app, '_echo_cancellation_available', return_value=True)):
            context.start()
            self.addCleanup(context.stop)
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:%d' % self.server.server_port

    def request(self, path, body=None):
        request = urllib.request.Request(self.url + path, headers={'Content-Type': 'application/json'},
                                         data=json.dumps(body).encode() if body is not None else None)
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_switch_is_remembered_and_reported(self):
        status, body = self.request('/api/echo-cancel', {'enabled': False})
        self.assertEqual(status, 200)
        self.assertFalse(body['enabled'])
        self.assertEqual(self.saved[-1]['echo_cancel'], False)
        self.assertFalse(self.request('/api/state')[1]['echo_cancel']['enabled'])

    def test_switch_requires_a_boolean(self):
        self.assertEqual(self.request('/api/echo-cancel', {'enabled': 'yes'})[0], 400)
        self.assertEqual(self.saved, [])


if __name__ == '__main__':
    unittest.main()
