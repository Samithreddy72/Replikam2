#!/usr/bin/env python3
"""Real webrtcdsp echo cancellation between the return player and the in-process microphone.

Runs in the linux-media suite: Ubuntu noble disabled webrtcdsp in its package, jammy ships it.
A skip here blocks the suite on purpose; the job installs the element it needs.
"""
import pathlib
import socket
import struct
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/netbridge-source'))
from audio_engine import AudioEngine, ECHO_PROBE, MicEngine, echo_cancellation_available, gst_runtime


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class EchoCancellation(unittest.TestCase):
    def setUp(self):
        try:
            self.Gst = gst_runtime()
        except ImportError as e:
            self.skipTest('GI/GStreamer required: ' + str(e))
        if not echo_cancellation_available():
            self.skipTest('webrtcdsp/webrtcechoprobe not installed')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.port = free_port()
        settings = dict(gain=1., jitter_ms=80, dynamics=False, sink_sync=True, fec=True, plc=False)
        self.engine = AudioEngine(self.port, settings, self.tmp.name, sink='fakesink')
        self.addCleanup(self.engine.terminate)
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.receiver.close)
        self.receiver.bind(('127.0.0.1', 0))
        self.receiver.settimeout(.2)

    def microphone(self, **kw):
        mic = MicEngine('Test microphone', '127.0.0.1', self.receiver.getsockname()[1],
                        source='audiotestsrc is-live=true wave=sine samplesperbuffer=480', **kw)
        self.addCleanup(mic.terminate)
        return mic

    def receive_contiguous(self, seconds=2):
        packets = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                data = self.receiver.recv(65535)
            except socket.timeout:
                continue
            self.assertEqual(data[1] & 0x7f, 97)
            packets.append(struct.unpack('!HI', data[2:8]))
        self.assertGreater(len(packets), 40)
        for a, b in zip(packets[2:-2], packets[3:-1]):
            self.assertEqual((b[0] - a[0]) & 65535, 1)
            self.assertEqual((b[1] - a[1]) & 0xffffffff, 960)

    def test_canceller_runs_against_the_return_players_probe(self):
        Gst = self.Gst
        self.assertEqual(self.engine.echo_probe, ECHO_PROBE)
        self.assertIsNotNone(self.engine.pipeline.get_by_name(ECHO_PROBE))
        # Meeting audio arriving and playing while the presenter talks: both directions live.
        sender = Gst.parse_launch('audiotestsrc is-live=true wave=sine samplesperbuffer=960 ! '
            'audio/x-raw,rate=48000,channels=2 ! audioconvert ! opusenc frame-size=20 ! '
            'rtpopuspay pt=97 ! udpsink host=127.0.0.1 port=%d sync=false' % self.port)
        self.addCleanup(lambda: sender.set_state(Gst.State.NULL))
        sender.set_state(Gst.State.PLAYING)
        mic = self.microphone(echo_probe=self.engine.echo_probe)
        self.assertIsNotNone(mic.pipeline.get_by_name('aec'))
        self.receive_contiguous()
        self.assertTrue(mic.snapshot()['echo_cancellation'])
        self.assertIsNone(mic.poll())
        snapshot = self.engine.snapshot()
        self.assertTrue(snapshot['running'], snapshot)
        self.assertGreater(snapshot['packets']['packets'], 20)

    def test_silent_meeting_never_stalls_the_microphone(self):
        # Nothing arrives from the bridge (quiet room): the probe records nothing, and the
        # presenter must still be heard.
        self.microphone(echo_probe=self.engine.echo_probe)
        self.receive_contiguous()

    def test_missing_probe_fails_at_start_so_the_session_can_fall_back(self):
        with self.assertRaises(RuntimeError):
            MicEngine('Test microphone', '127.0.0.1', 9, echo_probe='no_such_probe',
                      source='audiotestsrc is-live=true')


if __name__ == '__main__':
    unittest.main()
