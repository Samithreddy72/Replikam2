#!/usr/bin/env python3
"""Decode current-session RTP into a single atomic RAM frame. Never render status text.

UVC owns the freeze/black deadline independently of this process. No video failure
restarts voice, return audio, the gadget or the UVC pump. Requires the matching presenter.
"""
import os
import json
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
            and struct.unpack('!I', packet[8:12])[0] == (((epoch >> 32) & 0x7fffffff) or 1))


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
                '! rtpjitterbuffer name=jitter latency=%d drop-on-latency=true do-lost=true post-drop-messages=true drop-messages-interval=1000 '
                '! rtph264depay wait-for-keyframe=true ! video/x-h264,alignment=au '
                # Keep decoder scheduling off the jitter-buffer worker. Two
                # complete access units bound memory/backlog without tearing H264.
                '! queue name=decode_queue max-size-buffers=2 max-size-bytes=0 max-size-time=66666666 '
                '! h264parse name=parser ! avdec_h264 name=decoder max-threads=1 ! videoconvert ! videoscale '
                # The STATIC UVC source reads only the atomic RAM frame. A legacy
                # loopback sink adds driver contention and can stall decoded delivery.
                '! video/x-raw,format=YUY2,width=424,height=240 '
                # Isolate atomic publication from RTP/decode scheduling. Only the
                # newest complete decoded frame waits; never queue encoded history.
                '! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream '
                '! appsink name=frames emit-signals=true sync=false max-buffers=1 drop=true' % latency
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
            self.next_stats = 0
            self.counts = {"rtp_frames": 0, "parsed_frames": 0, "decoder_frames": 0, "samples": 0, "corrupt_samples": 0, "invalid_size": 0, "drop_on_latency": 0, "drop_too_late": 0}
            self.last_rtp_timestamp = None

        def stop(self):
            if self.pipe:
                self.pipe.set_state(Gst.State.NULL)
                bus = self.pipe.get_bus()
                bus.remove_signal_watch()
                self.pipe = None

        def fault(self, *args):
            if len(args) > 1 and args[1].type == Gst.MessageType.ERROR:
                error, _ = args[1].parse_error()
                print('Video pipeline error: %s' % error.message, flush=True)
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
            timestamp = struct.unpack('!I', buf.extract_dup(4, 4))[0]
            if timestamp != self.last_rtp_timestamp:
                self.counts['rtp_frames'] += 1
                self.last_rtp_timestamp = timestamp
            return Gst.PadProbeReturn.OK

        def stage(self, pad, info, name):
            self.counts[name] += 1
            return Gst.PadProbeReturn.OK

        def frame(self, sink, epoch):
            sample = sink.emit('pull-sample')
            self.counts['samples'] += int(sample is not None)
            if sample and self.epoch == epoch and read_session(control) == epoch:
                buf = sample.get_buffer()
                if buf.has_flags(Gst.BufferFlags.CORRUPTED):
                    self.counts["corrupt_samples"] += 1
                else:
                    # No videorate before this point: a static slide is fresh only when decoded.
                    pixels = buf.extract_dup(0, buf.get_size())
                    if len(pixels) != FRAME_BYTES:
                        self.counts["invalid_size"] += 1
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
            for element, key in (('parser', 'parsed_frames'), ('decoder', 'decoder_frames')):
                stage = self.pipe.get_by_name(element)
                if stage is not None:
                    stage.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, self.stage, key)
            bus = self.pipe.get_bus()
            bus.add_signal_watch()
            bus.connect('message::element', self.drop_message)
            bus.connect('message::error', self.fault)
            bus.connect('message::eos', self.fault)
            if self.pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                self.fault()

        def drop_message(self, bus, message):
            value = message.get_structure()
            if value is not None and value.get_name() == 'drop-msg':
                for field, key in (('num-drop-on-latency', 'drop_on_latency'), ('num-too-late', 'drop_too_late')):
                    if value.has_field(field):
                        self.counts[key] += int(value.get_value(field))

        def statistics(self):
            if not self.pipe or time.monotonic() < self.next_stats:
                return
            self.next_stats = time.monotonic() + 2
            jitter = self.pipe.get_by_name('jitter')
            if jitter is None:
                return
            stats = jitter.get_property('stats')
            values = {key: stats.get_value(key) for key in
                      ('num-pushed', 'num-lost', 'num-late', 'num-duplicates', 'avg-jitter')
                      if stats.has_field(key)}
            values.update(self.counts)
            values.update(decoded_frames=self.frames, monotonic_ns=time.monotonic_ns())
            try:
                tmp = directory / 'receiver-stats.tmp'
                tmp.write_text(json.dumps(values))
                os.replace(tmp, directory / 'receiver-stats.json')
            except OSError:
                pass  # Diagnostics must never stop media.

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
            self.statistics()
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
