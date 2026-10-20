#!/usr/bin/env python3
"""The Mac video leg asks the camera for a mode it actually offers.

OBS Virtual Camera offers only 1920x1080 at 60 fps. The fixed 1280x720@30 request failed to open
it, the video leg died on every repair while voice kept running, and the room saw black. No
camera or ffmpeg is needed: the listing below is ffmpeg's real output for OBS Virtual Camera.
  python3 tests/test-mac-camera-modes.py
"""
import pathlib, subprocess, sys, tempfile, unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'app/netbridge-source'))
import source_app as app

OBS = """[avfoundation @ 0x156607540] Selected video size (1x1) is not supported by the device.
[avfoundation @ 0x156607540] Supported modes:
[avfoundation @ 0x156607540]   1920x1080@[60.000000 60.000000]fps
[in#0 @ 0x600003c44400] Error opening input: Input/output error
Error opening input file 1:none.
"""
FACETIME = """[avfoundation @ 0x1] Supported modes:
[avfoundation @ 0x1]   1920x1080@[1.000000 30.000000]fps
[avfoundation @ 0x1]   1280x720@[1.000000 30.000000]fps
[avfoundation @ 0x1]   640x480@[1.000000 30.000000]fps
"""


def listing(*modes):
    return "".join("[avfoundation @ 0x1]   %s@[%s]fps\n" % m for m in modes)


class PickMode(unittest.TestCase):
    def test_obs_virtual_camera_gets_its_only_mode(self):
        self.assertEqual(app.pick_capture_mode(OBS), ('1920x1080', '60'))

    def test_proven_mode_kept_whenever_offered(self):
        self.assertEqual(app.pick_capture_mode(FACETIME), app.MAC_CAPTURE_MODE)
        self.assertEqual(app.MAC_CAPTURE_MODE, ('1280x720', '30'))

    def test_rate_is_the_top_of_a_range(self):
        # avfoundation matches the requested rate against each range's MAXIMUM only.
        self.assertEqual(app.pick_capture_mode(listing(('1280x720', '1.000000 60.000000'))),
                         ('1280x720', '60'))
        self.assertEqual(app.pick_capture_mode(listing(('1920x1080', '29.970030 29.970030'))),
                         ('1920x1080', '29.97'))

    def test_prefers_no_repeated_frames_then_no_enlarging_then_no_stretching(self):
        self.assertEqual(app.pick_capture_mode(listing(('1280x720', '1.000000 25.000000'),
                                                       ('1920x1080', '60.000000 60.000000'))),
                         ('1920x1080', '60'))
        self.assertEqual(app.pick_capture_mode(listing(('320x180', '1.000000 30.000000'),
                                                       ('1920x1080', '1.000000 30.000000'))),
                         ('1920x1080', '30'))
        self.assertEqual(app.pick_capture_mode(listing(('640x480', '1.000000 30.000000'),
                                                       ('3840x2160', '1.000000 30.000000'))),
                         ('3840x2160', '30'))
        # Same shape, size and rate class: the least work.
        self.assertEqual(app.pick_capture_mode(listing(('3840x2160', '1.000000 30.000000'),
                                                       ('1280x720', '60.000000 60.000000'))),
                         ('1280x720', '60'))

    def test_unusable_listing_is_none(self):
        for text in ("", None, "Error opening input file 0:none.\n", listing(('0x0', '0.000000 0.000000'))):
            self.assertIsNone(app.pick_capture_mode(text))


class Probe(unittest.TestCase):
    def test_probe_asks_without_capturing(self):
        with patch.object(app.subprocess, 'run', return_value=Mock(stderr=OBS)) as run:
            self.assertEqual(app.avfoundation_video_mode('ffmpeg', '1'), ('1920x1080', '60'))
        argv, kw = run.call_args
        argv = argv[0]
        self.assertEqual(argv[argv.index('-video_size') + 1], '1x1')
        self.assertEqual(argv[argv.index('-i') + 1], '1:none')
        self.assertEqual(argv[argv.index('-frames:v') + 1], '1')
        self.assertEqual((kw['encoding'], kw['timeout']), ('utf-8', 5))

    def test_probe_failure_is_none(self):
        for err in (subprocess.TimeoutExpired('ffmpeg', 5), FileNotFoundError('ffmpeg')):
            with patch.object(app.subprocess, 'run', side_effect=err):
                self.assertIsNone(app.avfoundation_video_mode('ffmpeg', '1'))


class MacSession(unittest.TestCase):
    def setUp(self):
        self.session = app.Session(); self.session.return_on = False
        self.spawned, self.probes, self.listing = [], [], OBS
        def spawn(argv, **kw):
            p = Mock(); p.poll.return_value = None; p.argv = argv; self.spawned.append(p); return p
        def run(argv, **kw):
            self.probes.append(argv)
            if isinstance(self.listing, Exception):
                raise self.listing
            return Mock(stderr=self.listing)
        logs = tempfile.TemporaryDirectory(); self.addCleanup(logs.cleanup)
        for ctx in (patch.object(app, 'IS_WIN', False), patch.object(app, 'IS_MAC', True),
                    patch.object(app, '_ffmpeg', return_value='ffmpeg'), patch.object(app, '_gst', return_value=None),
                    patch.object(app.subprocess, 'Popen', side_effect=spawn), patch.object(app.subprocess, 'run', side_effect=run),
                    patch.object(app, '_logdir', return_value=pathlib.Path(logs.name)),
                    patch.object(app.PINS, 'ticket', return_value='test-ticket'),
                    patch.object(app, 'av_devices', return_value={'video': [{'index': '1', 'name': 'OBS Virtual Camera'}]}),
                    patch.object(app, '_quit', side_effect=lambda p, **k: setattr(p.poll, 'return_value', 0))):
            ctx.start(); self.addCleanup(ctx.stop)
        self.addCleanup(self.session.stop)

    def start(self, **kw):
        self.session.start('127.0.0.1', '1', '0', **kw)
        self.session.capture_names = {'video': 'OBS Virtual Camera', 'voice': 'Test mic'}

    def video(self):
        return self.session.leg_proc['video'].argv

    def mode(self, argv):
        return argv[argv.index('-video_size') + 1], argv[argv.index('-framerate') + 1]

    def test_go_live_captures_obs_in_its_own_mode(self):
        self.start()
        self.assertEqual(self.mode(self.video()), ('1920x1080', '60'))
        self.assertEqual(self.video()[self.video().index('-i') + 1], '1:none')
        # Still scaled and resampled to the bridge's stream, whatever the camera delivers.
        self.assertIn('scale=%d:%d,format=nv12' % (app.STREAM_W, app.STREAM_H), self.video())
        self.assertEqual(self.video()[self.video().index('-r') + 1], str(app.STREAM_FPS))
        # The camera-off stream does not depend on the camera at all.
        self.assertNotIn('-video_size', self.session.black_argv)
        self.assertIn('lavfi', self.session.black_argv)

    def test_unanswered_probe_keeps_the_proven_mode(self):
        self.listing = subprocess.TimeoutExpired('ffmpeg', 5)
        self.start()
        self.assertEqual(self.mode(self.video()), app.MAC_CAPTURE_MODE)

    def test_camera_off_never_asks_and_unmute_asks(self):
        self.start(video_muted=True)
        self.assertEqual(self.probes, [])
        self.assertFalse(self.session.set_video_muted(False))
        self.assertEqual(len(self.probes), 1)
        self.assertEqual(self.mode(self.video()), ('1920x1080', '60'))

    def test_repair_keeps_the_mode_when_the_camera_cannot_answer(self):
        self.start()
        self.listing = subprocess.TimeoutExpired('ffmpeg', 5)
        self.assertTrue(self.session.respawn_leg('video'))
        self.assertEqual(self.mode(self.video()), ('1920x1080', '60'))

    def test_quality_change_keeps_the_camera_mode(self):
        self.start()
        argv = list(self.session.leg_argv['video'])
        argv[argv.index('-r') + 1] = '20'
        self.session.leg_argv['video'] = argv
        self.assertTrue(self.session.respawn_leg('video'))
        self.assertEqual(self.mode(self.video()), ('1920x1080', '60'))
        self.assertEqual(self.video()[self.video().index('-r') + 1], '20')


if __name__ == '__main__':
    unittest.main()
