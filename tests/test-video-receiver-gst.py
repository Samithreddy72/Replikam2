#!/usr/bin/env python3
"""Real H264/RTP decode and session handover over loopback; no Pi/USB required."""
import importlib.util
import pathlib
import socket
import subprocess
import shutil
import tempfile
import time
import unittest
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
Gst.init(None)
ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('receiver', ROOT/'pi/scripts/bridge-video-receiver.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
EPOCH = 0x1234567812345678

class Decode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = pathlib.Path(self.tmp.name)
        self.control = self.directory/'session'
        self.control.write_text('%x 1 %d\n' % (EPOCH, time.monotonic_ns()+120_000_000_000))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(('127.0.0.1',0)); self.port = s.getsockname()[1]
        self.receiver = r.make_receiver(Gst, GLib, 20, self.control, self.directory,
            lambda latency: r.pipeline_description(latency).replace('port=5000','port=%d'%self.port))
        self.addCleanup(self.receiver.stop)
        self.receiver.tick()

    def drive(self, seconds):
        until = time.monotonic()+seconds
        ctx = GLib.MainContext.default()
        while time.monotonic()<until:
            while ctx.pending(): ctx.iteration(False)
            self.receiver.tick()
            time.sleep(.005)

    def sender(self, ssrc):
        p = Gst.parse_launch('videotestsrc is-live=true pattern=black ! '
            'video/x-raw,width=424,height=240,framerate=10/1 ! '
            'x264enc tune=zerolatency key-int-max=10 ! rtph264pay pt=96 config-interval=-1 ssrc=%d ! '
            'udpsink host=127.0.0.1 port=%d sync=false' % (ssrc,self.port))
        self.addCleanup(lambda:p.set_state(Gst.State.NULL))
        self.assertNotEqual(p.set_state(Gst.State.PLAYING),Gst.StateChangeReturn.FAILURE)
        return p

    def test_static_decoded_frames_advance_then_stop_on_sender_crash(self):
        p = self.sender(EPOCH>>32)
        self.drive(.7)
        first = (self.directory/'frame').read_bytes()
        self.drive(.3)
        second = (self.directory/'frame').read_bytes()
        self.assertEqual(len(second), 96+r.FRAME_BYTES)
        self.assertEqual(first[96:], second[96:])
        self.assertNotEqual(first[:96], second[:96])
        p.set_state(Gst.State.NULL)
        self.drive(.3)
        stopped = (self.directory/'frame').read_bytes()
        self.drive(.3)
        self.assertEqual((self.directory/'frame').read_bytes(),stopped)
        self.assertEqual(self.receiver.failures,0)

    def test_slow_publication_does_not_block_rtp_decode(self):
        original = r.publish_frame
        def slow_publish(*args, **kwargs):
            time.sleep(.15)
            return original(*args, **kwargs)
        r.publish_frame = slow_publish
        self.addCleanup(setattr, r, 'publish_frame', original)
        p = Gst.parse_launch('videotestsrc is-live=true pattern=black ! '
            'video/x-raw,width=424,height=240,framerate=30/1 ! '
            'x264enc tune=zerolatency key-int-max=30 ! rtph264pay pt=96 config-interval=-1 ssrc=%d ! '
            'udpsink host=127.0.0.1 port=%d sync=false' % (EPOCH>>32,self.port))
        self.addCleanup(lambda:p.set_state(Gst.State.NULL))
        p.set_state(Gst.State.PLAYING)
        self.drive(1.3)
        self.assertGreaterEqual(self.receiver.counts['decoder_frames'], 28)
        self.assertLess(self.receiver.frames, self.receiver.counts['decoder_frames'])
        self.assertEqual(self.receiver.counts['drop_on_latency'], 0)

    def test_ffmpeg_high_bit_hash_matches_receiver(self):
        # Use a hash with the high bit set; transport masks it for FFmpeg compatibility.
        epoch = 0xf234567812345678
        self.control.write_text('%x 1 %d\n' % (epoch,time.monotonic_ns()+120_000_000_000))
        self.receiver.tick()
        ff = shutil.which('ffmpeg')
        self.assertIsNotNone(ff, 'real FFmpeg required')
        p = subprocess.Popen([ff,'-hide_banner','-loglevel','error','-re','-f','lavfi',
            '-i','color=c=black:s=424x240:r=10','-t','2','-c:v','libx264','-tune','zerolatency',
            '-g','10','-ssrc',str((epoch>>32)&0x7fffffff),'-f','rtp',
            'rtp://127.0.0.1:%d?pkt_size=1100'%self.port],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        self.addCleanup(lambda:p.poll() is None and p.kill())
        self.drive(1)
        self.assertTrue((self.directory/'frame').exists())
        _, err = p.communicate(timeout=5)
        self.assertEqual(p.returncode,0,err.decode())

    def test_old_presenter_packets_never_publish(self):
        self.sender((EPOCH>>32)+1)
        self.drive(.5)
        self.assertFalse((self.directory/'frame').exists())
        self.sender(EPOCH>>32)
        self.drive(.7)
        self.assertTrue((self.directory/'frame').exists())
        self.control.write_text('0 0 0\n')
        self.drive(.2)
        self.assertIsNone(self.receiver.pipe)
        stopped = (self.directory/'frame').read_bytes()
        self.drive(.2)
        self.assertEqual((self.directory/'frame').read_bytes(),stopped)

if __name__ == '__main__': unittest.main()
