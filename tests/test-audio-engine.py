#!/usr/bin/env python3
"""Real RTP/GStreamer integration; no sound device or meeting is required."""
import importlib.util
import json
import pathlib
import socket
import struct
import sys
import tempfile
import time
import unittest
import wave
from unittest.mock import patch

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app/netbridge-source'))
from audio_diagnostics import PacketTiming, Capture
from audio_engine import AudioEngine, gst_runtime, mac_microphone_argv


def port():
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]


class Diagnostics(unittest.TestCase):
    def test_sequence_wrap_reorder_and_ssrc(self):
        timing=PacketTiming()
        def pkt(seq,ts,ssrc=1):return struct.pack('!BBHII',128,97,seq,ts,ssrc)+b'abc'
        for i,(seq,ts) in enumerate([(65535,2**32-960),(0,0),(2,1920),(1,960)]):
            timing.add(pkt(seq,ts),i*.02)
        timing.add(pkt(0,0,2),.1)
        s=timing.snapshot()
        self.assertEqual(s['sequence_gap_observations'],1)
        self.assertEqual(s['reordered_or_duplicate'],1)
        self.assertEqual(s['ssrc_changes'],1)
        self.assertEqual(s['timestamp_jump_observations'],0)

    def test_capture_limits_and_dump_format(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):Capture(td,61,{})
            c=Capture(td,1,{})
            data=struct.pack('!BBHII',128,97,4,960,1)+b'abc'
            c.push('rtp',data);c.push('pcm',b'\0'*1920,0);c.stop()
            self.assertTrue(c.snapshot()['valid'])
            report=json.loads((c.directory/'manifest.json').read_text())
            self.assertFalse(report['capture']['active'])
            with (c.directory/'input.rtpdump').open('rb') as f:
                self.assertTrue(f.readline().startswith(b'#!rtpplay1.0'))
                f.read(16);size,plen,ms=struct.unpack('!HHI',f.read(8))
                self.assertEqual((size,plen),(len(data)+8,len(data)))
                self.assertEqual(f.read(plen),data)
            with wave.open(str(c.directory/'decoded.wav')) as w:self.assertEqual(w.getnframes(),480)


class Media(unittest.TestCase):
    def setUp(self):
        try:self.Gst=gst_runtime()
        except (ImportError,ValueError) as e:self.skipTest('GI/GStreamer required: '+str(e))
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.port=port()
        self.settings=dict(gain=1.,jitter_ms=80,dynamics=False,sink_sync=True,fec=True,plc=False)
        self.engine=AudioEngine(self.port,self.settings,self.tmp.name,sink='fakesink')
        self.addCleanup(self.engine.terminate)

    def test_forward_microphone_codec_caps_and_contiguous_rtp(self):
        # Device discovery is mocked; the generated conversion/Opus/RTP chain
        # runs for real. This catches incompatible encoder caps before deployment.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(('127.0.0.1', 0))
            receiver.settimeout(.2)
            with patch('audio_engine.mac_microphone_device_id', return_value=42):
                argv = mac_microphone_argv('gst-launch', 'Test microphone',
                                          '127.0.0.1', receiver.getsockname()[1])
            # Physical CoreAudio identity resolution is covered by the live device test.
            # Replace only the physical capture device, keeping its downstream chain.
            sender = self.Gst.parse_launchv([
                'audiotestsrc', 'is-live=true', 'num-buffers=100',
                'samplesperbuffer=480'] + argv[6:])
            self.addCleanup(lambda: sender.set_state(self.Gst.State.NULL))
            sender.set_state(self.Gst.State.PLAYING)
            packets = []
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try: data = receiver.recv(65535)
                except socket.timeout: continue
                packets.append(struct.unpack('!HI', data[2:8]))
            self.assertGreater(len(packets), 40)
            # The initial Opus pre-skip and final partial frame are legitimate.
            for a, b in zip(packets[2:-2], packets[3:-1]):
                self.assertEqual((b[0] - a[0]) & 65535, 1)
                self.assertEqual((b[1] - a[1]) & 0xffffffff, 960)

    def test_real_opus_capture_and_live_tuning(self):
        Gst=self.Gst
        sender=Gst.parse_launch('audiotestsrc is-live=true wave=sine samplesperbuffer=960 ! '
          'audio/x-raw,rate=48000,channels=2 ! audioconvert ! opusenc frame-size=20 ! '
          'rtpopuspay pt=97 ! udpsink host=127.0.0.1 port=%d sync=false'%self.port)
        self.addCleanup(lambda:sender.set_state(Gst.State.NULL))
        sender.set_state(Gst.State.PLAYING)
        self.engine.start_capture(1)
        deadline=time.monotonic()+4
        while time.monotonic()<deadline and not self.engine.capture.done.is_set():time.sleep(.05)
        before=self.engine.snapshot()
        self.assertGreater(before['packets']['packets'],20)
        self.assertTrue(before['capture']['valid'],before)
        pipeline=self.engine.pipeline
        for ms in (100,120,80):
            self.engine.tune(dict(self.settings,gain=.5,jitter_ms=ms))
        time.sleep(.15)
        after=self.engine.snapshot()
        self.assertIs(self.engine.pipeline,pipeline)
        self.assertGreater(after['packets']['packets'],before['packets']['packets'])
        self.assertEqual(after['settings']['gain'],.5)
        self.assertEqual(self.engine.elements['gain'].get_property('volume'),.5)
        self.assertEqual(self.engine.elements['compressor'].get_property('ratio'),1.)
        self.assertTrue(after['running'])
        for mode, value in (('none', 0), ('slave', 1)):
            self.engine.tune(dict(self.settings, clock_mode=mode))
            self.assertEqual(int(self.engine.elements['jitter'].get_property('mode')), value)
            self.assertIs(self.engine.pipeline, pipeline)
        with wave.open(str(self.engine.capture.directory/'decoded.wav')) as w:
            self.assertGreater(w.getnframes(),20000)
            self.assertEqual(w.getframerate(),48000)

    def test_loss_and_late_are_observable(self):
        # Valid 20 ms Opus silence frames; deliberately omit seq=10, then send it too late.
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
            for i in range(35):
                if i!=10:s.sendto(struct.pack('!BBHII',128,97,i,i*960,99)+b'\xf8\xff\xfe',('127.0.0.1',self.port))
                time.sleep(.02)
            s.sendto(struct.pack('!BBHII',128,97,10,9600,99)+b'\xf8\xff\xfe',('127.0.0.1',self.port))
        time.sleep(.3)
        snapshot=self.engine.snapshot()
        self.assertGreaterEqual(snapshot['jitter']['num-lost'],1,snapshot)
        self.assertGreaterEqual(snapshot['jitter']['num-late'],1,snapshot)
        self.assertEqual(snapshot['packets']['sequence_gap_observations'],1)

    def test_shutdown_releases_port_and_closes_capture(self):
        self.engine.start_capture(60)
        self.engine.terminate()
        self.assertEqual(self.engine.poll(),0)
        self.assertTrue(self.engine.capture.done.is_set())
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:s.bind(('127.0.0.1',self.port))


if __name__=='__main__':unittest.main()
