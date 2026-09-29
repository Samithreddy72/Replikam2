#!/usr/bin/env python3
"""Decode current-session RTP into a single atomic RAM frame. Never render status text.

UVC owns the freeze/black deadline independently of this process. No video failure
restarts voice, return audio, the gadget or the UVC pump. Requires the matching presenter.
"""
import os
from pathlib import Path
import struct
import sys
import time

FRAME_BYTES = 424 * 240 * 2
CONTROL = Path('/run/bridge-pin/video-session')
FRAME_DIR = Path('/run/bridge-video')


def read_session(path=CONTROL, now=None):
    try:
        epoch, active, deadline = path.read_text().split()
        epoch = int(epoch, 16)
        if int(active) == 1 and epoch and (time.monotonic_ns() if now is None else now) < int(deadline):
            return epoch
    except (OSError, ValueError):
        pass
    return 0


def rtp_current(packet, epoch):
    # Fixed RTP header includes SSRC even when extensions/CSRCs are present.
    return (len(packet) >= 12 and packet[0] >> 6 == 2 and packet[1] & 127 == 96
            and struct.unpack('!I', packet[8:12])[0] == ((epoch >> 32) or 1))


def publish_frame(pixels, epoch, stamp, directory=FRAME_DIR, count=0):
    if len(pixels) != FRAME_BYTES:
        return False
    header = ('NBV1 %016x %020d %010d %010d\n' % (epoch, stamp, len(pixels), count)).encode().ljust(96, b' ')
    # tmpfs, single writer, atomic replacement; never writes a captured picture to /data.
    tmp = directory / 'frame.tmp'
    with tmp.open('wb') as f:
        f.write(header)
        f.write(pixels)
    os.replace(tmp, directory / 'frame')
    return True


def pipeline_description(latency):
    return (
                'udpsrc name=net port=5000 buffer-size=262144 caps="application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000" '
                '! rtpjitterbuffer latency=%d drop-on-latency=true do-lost=true '
                '! rtph264depay wait-for-keyframe=true ! video/x-h264,alignment=au '
                '! h264parse ! avdec_h264 ! videoconvert ! videoscale '
                '! video/x-raw,format=YUY2,width=424,height=240 ! tee name=t '
                't. ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream '
                '! appsink name=frames emit-signals=true sync=false max-buffers=1 drop=true '
                't. ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream '
                '! videorate ! video/x-raw,framerate=30/1 ! v4l2sink device=/dev/video40 sync=false' % latency
    )


def make_receiver(Gst, GLib, latency, control=CONTROL, directory=FRAME_DIR,
                  pipeline_factory=pipeline_description):
    class Receiver:
        def __init__(self):
            self.pipe = None
            self.epoch = 0
            self.retry_at = 0
            self.failures = 0
            self.healthy_since = 0
            self.frames = 0

        def stop(self):
            if self.pipe:
                self.pipe.set_state(Gst.State.NULL)
                bus = self.pipe.get_bus()
                bus.remove_signal_watch()
                self.pipe = None

        def fault(self, *unused):
            self.stop()
            self.failures += 1
            self.healthy_since = 0
            self.retry_at = time.monotonic() + (2, 5, 10)[min(self.failures - 1, 2)]
            print('Video receiver unavailable; attempt %d/3' % self.failures, flush=True)
            return False

        def packet(self, pad, info, epoch):
            buf = info.get_buffer()
            if self.epoch != epoch or read_session(control) != epoch or not buf or not rtp_current(buf.extract_dup(0, min(buf.get_size(), 12)), epoch):
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        def frame(self, sink, epoch):
            sample = sink.emit('pull-sample')
            if sample and self.epoch == epoch and read_session(control) == epoch:
                buf = sample.get_buffer()
                if not buf.has_flags(Gst.BufferFlags.CORRUPTED):
                    # No videorate before this point: a static slide is fresh only when decoded.
                    pixels = buf.extract_dup(0, buf.get_size())
                    try:
                        if publish_frame(pixels, epoch, time.monotonic_ns(), directory, self.frames + 1):
                            self.frames += 1
                            if not self.healthy_since:
                                self.healthy_since = time.monotonic()
                            elif time.monotonic() - self.healthy_since >= 60:
                                self.failures = 0
                    except OSError:
                        GLib.idle_add(self.fault)
            return Gst.FlowReturn.OK

        def start(self):
            epoch = self.epoch
            self.pipe = Gst.parse_launch(
                pipeline_factory(latency))
            self.pipe.get_by_name('net').get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, self.packet, epoch)
            self.pipe.get_by_name('frames').connect('new-sample', self.frame, epoch)
            bus = self.pipe.get_bus()
            bus.add_signal_watch()
            bus.connect('message::error', self.fault)
            bus.connect('message::eos', self.fault)
            if self.pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                self.fault()

        def tick(self):
            epoch = read_session(control)
            if epoch != self.epoch:
                self.stop()  # Discard socket/jitter/decoder buffers at every session boundary.
                self.epoch = epoch
                self.failures = 0
                self.retry_at = 0
                self.healthy_since = 0
            if epoch and not self.pipe and self.failures < 3 and time.monotonic() >= self.retry_at:
                try:
                    self.start()
                except Exception as exc:
                    print('Video pipeline failed: %s' % type(exc).__name__, flush=True)
                    self.fault()
            return True

    return Receiver()


def main():
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst, GLib
    Gst.init(None)
    latency = min(100, max(0, int(sys.argv[1] if len(sys.argv) > 1 else 100)))
    FRAME_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    receiver = make_receiver(Gst, GLib, latency)
    GLib.timeout_add(100, receiver.tick)
    try:
        GLib.MainLoop().run()
    finally:
        receiver.stop()


if __name__ == '__main__':
    main()
