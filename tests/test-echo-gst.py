#!/usr/bin/env python3
"""Real webrtcdsp echo cancellation: the microphone as a branch of the return player's pipeline.

Runs in the linux-media suite: Ubuntu noble disabled webrtcdsp in its package, jammy ships it.
A skip here blocks the suite on purpose; the job installs the element it needs.
"""
import collections
import faulthandler
import math
import pathlib
import random
import re
import socket
import statistics
import struct
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/netbridge-source'))
from audio_engine import AudioEngine, ECHO_PROBE, MicEngine, echo_cancellation_available, gst_runtime

TONE = 'audiotestsrc is-live=true wave=sine samplesperbuffer=480'
SETTINGS = dict(gain=1., jitter_ms=80, dynamics=False, sink_sync=True, fec=True, plc=False)


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
        self.engine = self.player()
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.receiver.close)
        self.receiver.bind(('127.0.0.1', 0))
        self.receiver.settimeout(.2)

    def player(self, port=None, **kw):
        engine = AudioEngine(port or self.port, dict(SETTINGS, **kw.pop('settings', {})),
                             self.tmp.name, sink='fakesink', **kw)
        self.addCleanup(engine.terminate)
        return engine

    def meeting_audio(self, port=None, extra=''):
        sender = self.Gst.parse_launch(
            'audiotestsrc is-live=true wave=sine samplesperbuffer=960 ! '
            'audio/x-raw,rate=48000,channels=2 ! audioconvert ! opusenc frame-size=20 ! '
            'rtpopuspay pt=97 ! udpsink host=127.0.0.1 port=%d sync=false' % (port or self.port) + extra)
        self.addCleanup(lambda: sender.set_state(self.Gst.State.NULL))
        sender.set_state(self.Gst.State.PLAYING)
        return sender

    def attach(self, engine=None, source=TONE):
        mic = (engine or self.engine).attach_microphone(
            'Test microphone', '127.0.0.1', self.receiver.getsockname()[1], source=source)
        self.addCleanup(mic.terminate)
        return mic

    def receive_contiguous(self, seconds=2):
        # Discard what an earlier microphone left in the socket: a new one starts its own
        # RTP sequence, so mixing the two would look like a gap.
        self.receiver.setblocking(False)
        try:
            while True:
                self.receiver.recv(65535)
        except BlockingIOError:
            pass
        self.receiver.settimeout(.2)
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

    def test_canceller_runs_inside_the_return_players_pipeline(self):
        self.meeting_audio()
        mic = self.attach()
        self.assertEqual(self.engine.echo_probe, ECHO_PROBE)
        self.assertIs(mic.branch.get_parent(), self.engine.pipeline)
        self.assertIsNotNone(mic.branch.get_by_name('aec'))
        self.receive_contiguous()
        self.assertIsNone(mic.poll())
        self.assertTrue(mic.snapshot()['echo_cancellation'])
        snapshot = self.engine.snapshot()
        self.assertTrue(snapshot['running'], snapshot)
        self.assertGreater(snapshot['packets']['packets'], 20)
        self.assertTrue(snapshot['microphone']['running'])

    def test_output_syncs_to_the_clock_while_cancelling(self):
        # The probe's timing comes from the LATENCY event that only a syncing sink sends.
        engine = self.player(port=free_port(), settings=dict(sink_sync=False))
        output = engine.elements['output']
        self.assertFalse(output.get_property('sync'))
        mic = self.attach(engine)
        self.assertTrue(output.get_property('sync'))
        mic.terminate()
        self.assertFalse(output.get_property('sync'))

    def test_silent_meeting_never_stalls_the_microphone(self):
        # Nothing arrives from the bridge (quiet room): the probe records nothing, and the
        # presenter must still be heard.
        self.attach()
        self.receive_contiguous()

    def test_failing_microphone_never_stops_meeting_audio(self):
        self.meeting_audio()
        with self.assertRaises(RuntimeError):
            self.attach(source='audiotestsrc is-live=true ! video/x-raw')   # cannot negotiate
        time.sleep(.5)
        snapshot = self.engine.snapshot()
        self.assertTrue(snapshot['running'], snapshot)
        self.assertIsNone(snapshot['failure'], snapshot)
        self.assertIsNone(snapshot['microphone'])
        self.assertLess(snapshot['last_pcm_age_ms'], 500, snapshot)
        # And the canceller can claim the probe again afterwards.
        self.attach()
        self.receive_contiguous(1)

    def test_microphone_can_detach_and_reattach(self):
        mic = self.attach()
        mic.terminate()
        self.assertEqual(mic.poll(), 0)
        self.assertIsNone(self.engine.microphone)
        self.attach()
        self.receive_contiguous(1)

    def test_stopping_meeting_audio_ends_its_microphone(self):
        mic = self.attach()
        self.engine.terminate()
        self.assertEqual(mic.poll(), 1)
        self.assertIn('stopped', mic.snapshot()['failure'])

    def test_player_without_probe_cannot_attach(self):
        engine = self.player(port=free_port(), echo_probe=False)
        with self.assertRaises(RuntimeError):
            self.attach(engine)

    def test_meeting_audio_reaches_a_float_only_output_device(self):
        # Windows' WASAPI output takes only its mix format (typically 32-bit float, often
        # 44.1 kHz). 0.0.104 fed it the probe's S16LE directly: no meeting audio on Windows.
        port = free_port()
        engine = self.player(port=port, device_caps='audio/x-raw,format=F32LE,layout=interleaved,rate=44100,channels=2')
        self.assertEqual(engine.echo_probe, ECHO_PROBE)
        self.meeting_audio(port)
        time.sleep(1.5)
        snapshot = engine.snapshot()
        self.assertTrue(snapshot['running'], snapshot)
        self.assertIsNone(snapshot['failure'], snapshot)
        self.assertGreater(snapshot['packets']['packets'], 20)
        self.assertIsNotNone(snapshot['last_pcm_age_ms'], snapshot)
        self.assertLess(snapshot['last_pcm_age_ms'], 500, snapshot)

    def speak(self, gate):
        """Gate the meeting audio like speech: syllable-length bursts and irregular pauses.

        AEC3 treats a STATIONARY render signal (steady noise) differently - it backs off
        suppressing echo that sounds like background noise - so steady noise is no proxy for
        a meeting. Same pattern every run (seeded), applied before the room's copy is taken.
        """
        pattern = random.Random(7)
        done = threading.Event()
        def run():
            while not done.is_set():
                gate.set_property('volume', 1.0)
                if done.wait(pattern.uniform(.1, .3)):
                    break
                gate.set_property('volume', 0.0)
                done.wait(pattern.uniform(.05, .4))
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(done.set)

    def echo_level(self, cancel, seconds=7, acoustic_ms=40, room_volume=0.18, talk=True):
        """Return the mean level (dBFS) of what the microphone sends to the bridge over the last
        2 seconds, while meeting audio plays and the room feeds it back into the microphone.

        The room is simulated as faithfully as CI allows: the microphone hears EXACTLY what the
        speaker plays (the probe's input - decoded, processed meeting audio), room_volume
        quieter, acoustic_ms after it plays. Each piece is stamped with that capture time and
        handed to the microphone only once that time has come, as a real microphone does (the
        canceller compares a capture stamped t with audio played just before t).
        """
        Gst = self.Gst
        port, out = free_port(), free_port()
        engine = self.player(port=port)
        sender = Gst.parse_launch(
            'audiotestsrc is-live=true wave=pink-noise volume=0.3 samplesperbuffer=480 ! '
            'audio/x-raw,rate=48000,channels=1 ! volume name=talk ! audioconvert ! '
            'audio/x-raw,channels=2 ! opusenc bitrate=128000 frame-size=10 ! rtpopuspay pt=97 ! '
            'udpsink host=127.0.0.1 port=%d sync=false' % port)
        self.addCleanup(lambda: sender.set_state(Gst.State.NULL))
        if talk:
            self.speak(sender.get_by_name('talk'))
        sender.set_state(Gst.State.PLAYING)
        # Meeting audio plays at its timestamp + the pipeline latency (jitter buffer etc.), and
        # the player only knows that latency once audio has reached its output.
        playout = None
        deadline = time.monotonic() + 5
        while playout is None and time.monotonic() < deadline:
            query = Gst.Query.new_latency()
            if engine.pipeline.query(query):
                live, minimum, _ = query.parse_latency()
                playout = minimum if live else None
            time.sleep(.1)
        self.assertIsNotNone(playout, 'meeting audio never started playing')
        lag = playout + acoustic_ms * Gst.MSECOND        # from timestamp to the microphone
        played = collections.deque()
        def heard(pad, info):
            buffer = info.get_buffer()
            played.append((buffer.pts, buffer.duration, buffer.extract_dup(0, buffer.get_size())))
            return Gst.PadProbeReturn.OK
        engine.pipeline.get_by_name(ECHO_PROBE).get_static_pad('sink').add_probe(
            Gst.PadProbeType.BUFFER, heard)
        clock, base = engine.pipeline.get_clock(), engine.pipeline.get_base_time()
        microphone = {}
        stop = threading.Event()
        def room():
            while not stop.is_set():
                now = clock.get_time() - base
                while played and played[0][0] + lag <= now:
                    pts, duration, data = played.popleft()
                    feed = microphone.get('feed')
                    if feed is None:
                        continue                  # no microphone yet: nobody hears this echo
                    buffer = Gst.Buffer.new_wrapped(data)
                    buffer.pts, buffer.duration = pts + lag, duration
                    feed.emit('push-buffer', buffer)
                stop.wait(.005)
        thread = threading.Thread(target=room, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(stop.set)
        source = ('appsrc name=room is-live=true format=time '
                  'caps="audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2" '
                  '! volume volume=%s' % room_volume)
        listener = Gst.parse_launch(
            'udpsrc port=%d caps="application/x-rtp,media=audio,encoding-name=OPUS,payload=97,'
            'clock-rate=48000" ! rtpjitterbuffer latency=50 ! rtpopusdepay ! opusdec ! '
            'audioconvert ! level interval=100000000 post-messages=true ! fakesink sync=false' % out)
        self.addCleanup(lambda: listener.set_state(Gst.State.NULL))
        listener.set_state(Gst.State.PLAYING)
        if cancel:
            mic = engine.attach_microphone('Room', '127.0.0.1', out, source=source)
            microphone['feed'] = mic.branch.get_by_name('room')
        else:
            mic = MicEngine('Room', '127.0.0.1', out, source=source)
            microphone['feed'] = mic.pipeline.get_by_name('room')
        self.addCleanup(mic.terminate)
        levels = []
        bus = listener.get_bus()
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ELEMENT)
            structure = message.get_structure() if message else None
            if structure is None or structure.get_name() != 'level':
                continue
            rms = re.search(r'rms=\(GValueArray\)<([^>]*)>', structure.to_string())
            if rms:
                values = [float(v) for v in rms.group(1).split(',')]
                levels.append((time.monotonic() - start, max(values)))
        self.assertTrue(engine.snapshot()['running'])
        late = [db for t, db in levels if t > seconds - 2]
        self.assertGreater(len(late), 5, levels)
        power = statistics.fmean(10 ** (db / 10) for db in late)
        print('\n%s [room x%s, %s]: playout %.0f ms, echo %d ms after it, levels %s' % (
            'canceller' if cancel else 'no canceller', room_volume, 'speech' if talk else 'steady',
            playout / Gst.MSECOND, acoustic_ms, ' '.join('%.0f' % db for t, db in levels)))
        return 10 * math.log10(power) if power > 0 else -120.

    def test_echo_of_meeting_audio_is_removed_from_the_microphone(self):
        # The control is the same room through a microphone with no canceller, so the drop
        # cannot come from anything but cancellation.
        echo = self.echo_level(cancel=False)
        cancelled = self.echo_level(cancel=True)
        print('\nmicrophone level: no canceller %.1f dBFS, canceller %.1f dBFS' % (echo, cancelled))
        self.assertGreater(echo, -60, 'the room did not reach the microphone; the test proves nothing')
        self.assertLess(cancelled, echo - 10)


if __name__ == '__main__':
    # A hang inside GStreamer must show WHERE (every thread's stack), not just a job timeout.
    faulthandler.dump_traceback_later(240, exit=True)
    unittest.main()
