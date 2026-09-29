#!/usr/bin/env python3
"""Execute the actual C room-output policy and Python producer using fault fixtures."""
import ctypes
import importlib.util
import pathlib
import struct
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('receiver', ROOT / 'pi/scripts/bridge-video-receiver.py')
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)
N = receiver.FRAME_BYTES
SECOND = 1_000_000_000
EPOCH = 0x123456789abcdef0

class Fallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        build = pathlib.Path(cls.build.name)
        src = build / 'test.c'
        src.write_text('#include "bridge-video-frame.h"\n'
                       'static struct nb_frame_cache cache;\n'
                       'void reset(void) { memset(&cache, 0, sizeof cache); }\n'
                       'void output(const char *c, const char *f, uint64_t now, unsigned char *out) '
                       '{ nb_frame_output(&cache,c,f,now,out,NB_FRAME_BYTES); }\n')
        subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        '-I', str(ROOT / 'sources'), str(src), '-o', str(build / 'policy.so')], check=True)
        cls.lib = ctypes.CDLL(str(build / 'policy.so'))
        cls.lib.output.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint64, ctypes.c_void_p]

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.control = self.dir / 'control'
        self.frame = self.dir / 'frame'
        self.lib.reset()
        self.activate()
        self.pixels = bytes([44, 128, 57, 128]) * (N // 4)
        self.black = bytes([16, 128]) * (N // 2)

    def activate(self, epoch=EPOCH, deadline=1000 * SECOND):
        self.control.write_text('%x 1 %d\n' % (epoch, deadline))

    def publish(self, stamp=10*SECOND, epoch=EPOCH):
        self.assertTrue(receiver.publish_frame(self.pixels, epoch, stamp, self.dir))

    def output(self, now):
        out = ctypes.create_string_buffer(N)
        self.lib.output(str(self.control).encode(), str(self.frame).encode(), now, out)
        return out.raw

    def test_missing_frame_is_exact_black(self):
        self.assertEqual(self.output(10*SECOND), self.black)

    def test_freeze_and_exact_sixty_second_deadline(self):
        self.publish()
        self.assertEqual(self.output(10*SECOND), self.pixels)
        self.frame.unlink()  # producer crash: UVC independently holds RAM cache
        self.assertEqual(self.output(70*SECOND-1), self.pixels)
        self.assertEqual(self.output(70*SECOND), self.black)

    def test_static_slide_is_fresh_when_decoded(self):
        for t in (10, 69, 128, 187):
            self.publish(t*SECOND)
            self.assertEqual(self.output(t*SECOND), self.pixels)

    def test_stop_clears_immediately(self):
        self.publish()
        self.output(10*SECOND)
        self.control.write_text('0 0 0\n')
        self.assertEqual(self.output(10*SECOND+1), self.black)

    def test_handover_rejects_previous_frame(self):
        self.publish()
        self.output(10*SECOND)
        self.activate(EPOCH+1)
        self.assertEqual(self.output(11*SECOND), self.black)
        self.publish(12*SECOND, EPOCH+1)
        self.assertEqual(self.output(12*SECOND), self.pixels)

    def test_expiry_does_not_depend_on_watcher(self):
        self.publish()
        self.activate(deadline=11*SECOND)
        self.assertEqual(self.output(11*SECOND), self.black)

    def test_corrupt_frame_does_not_replace_clean_cache(self):
        self.publish()
        self.output(10*SECOND)
        self.publish(11*SECOND)
        self.frame.write_bytes(self.frame.read_bytes()[:120])
        self.assertEqual(self.output(11*SECOND), self.pixels)
        self.lib.reset()
        self.assertEqual(self.output(11*SECOND), self.black)

    def test_pump_restart_recovers_only_unexpired_current_snapshot(self):
        self.publish()
        self.lib.reset()
        self.assertEqual(self.output(69*SECOND), self.pixels)
        self.lib.reset()
        self.assertEqual(self.output(70*SECOND), self.black)

    def test_future_frame_rejected(self):
        self.publish(20*SECOND)
        self.assertEqual(self.output(10*SECOND), self.black)

    def test_invalid_control_fails_black(self):
        self.publish()
        for value in ('', 'garbage', '123 1', '123 9 9999999999999'):
            self.control.write_text(value)
            self.assertEqual(self.output(10*SECOND), self.black)

    def test_rtp_previous_session_malformed_and_wrong_payload_rejected(self):
        packet = b'\x80\x60' + b'\x00'*6 + struct.pack('!I', EPOCH >> 32)
        self.assertTrue(receiver.rtp_current(packet, EPOCH))
        self.assertFalse(receiver.rtp_current(packet, EPOCH+(1 << 32)))
        self.assertFalse(receiver.rtp_current(packet[:10], EPOCH))
        self.assertFalse(receiver.rtp_current(b'\x80\x61'+packet[2:], EPOCH))

    def test_partial_decoded_frame_not_published(self):
        self.assertFalse(receiver.publish_frame(b'bad', EPOCH, 10*SECOND, self.dir))
        self.assertFalse(self.frame.exists())

    def test_receiver_control_expiry(self):
        self.assertEqual(receiver.read_session(self.control, 10*SECOND), EPOCH)
        self.assertEqual(receiver.read_session(self.control, 1000*SECOND), 0)

class Telemetry(unittest.TestCase):
    def test_reports_source_state_without_claiming_room_verification(self):
        spec = importlib.util.spec_from_file_location('web', ROOT/'pi/scripts/bridge-web.py')
        web = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(web)
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            control = directory/'control'
            frame = directory/'frame'
            control.write_text('%x 1 %d\n' % (EPOCH, 1000*SECOND))
            receiver.publish_frame(bytes(N), EPOCH, 10*SECOND, directory, 5)
            for now, state in ((10, 'live'), (12, 'frozen'), (69, 'frozen'), (70, 'black')):
                result = web.video_output_state(control, frame, now*SECOND)
                self.assertEqual(result['state'], state)
                self.assertFalse(result['receiver_verified'])
            control.write_text('corrupt 1 invalid')
            self.assertIsNone(web.video_output_state(control, frame, 10*SECOND))

if __name__ == '__main__':
    unittest.main()
