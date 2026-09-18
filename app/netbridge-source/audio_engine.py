"""Persistent GStreamer receiver with live properties, telemetry and opt-in capture.

PyGObject is optional for legacy packages. Source runs with GI use this engine by default.
No custom drift servo is layered over GStreamer's existing clock synchronization.
"""
import collections
import ctypes
import json
import os
import pathlib
import subprocess
import threading
import time
from audio_diagnostics import Capture, PacketTiming


def gst_runtime():
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    Gst.init(None)
    return Gst


def mac_microphone_uid(name):
    """Resolve the currently attached input, rejecting absent/ambiguous names."""
    Gst = gst_runtime()
    monitor = Gst.DeviceMonitor()
    monitor.add_filter('Audio/Source', None)
    if not monitor.start():
        raise RuntimeError('CoreAudio input discovery failed')
    try:
        matches = [d for d in monitor.get_devices() if d.get_display_name() == name]
        if len(matches) != 1:
            raise RuntimeError('Microphone name is missing or ambiguous: %s' % name)
        properties = matches[0].get_properties()
        uid = properties.get_string('unique-id') if properties else None
        if not uid:
            raise RuntimeError('Microphone has no CoreAudio unique ID')
    finally:
        monitor.stop()
    return uid


def mac_microphone_argv(executable, name, host, port, gain_db=0):
    """Use CoreAudio's capture ring and resolve identity on every launch."""
    # CoreAudio object IDs resolved in the frozen parent can differ from the
    # gst subprocess (observed parent=114, child=113). Let osxaudiosrc resolve
    # the default input in its own process; retain explicit selection for named inputs.
    device_id = 0 if name == 'System default microphone' else mac_microphone_device_id(name)
    argv = [executable, '-e', 'osxaudiosrc', 'device=%d' % device_id,
            'buffer-time=40000', 'latency-time=10000', '!',
            'audioconvert', '!', 'audioresample', '!',
            'audio/x-raw,rate=48000,channels=2,format=F32LE', '!']
    if gain_db:
        argv += ['volume', 'volume=%s' % (10 ** (float(gain_db) / 20)), '!']
    return argv + ['audioconvert', '!', 'audio/x-raw,format=S16LE', '!',
                   'opusenc', 'bitrate=64000', 'audio-type=voice', 'frame-size=20',
                   'inband-fec=true', 'packet-loss-percentage=5', '!',
                   'rtpopuspay', 'pt=97', '!', 'udpsink',
                   'host=' + json.dumps(host), 'port=%d' % int(port), 'sync=false',
                   'async=false']


def mac_microphone_device_id(name):
    """Resolve the current CoreAudio object ID, which changes on USB reattach.

    A nonzero explicit device prevents capture from following the default input.
    """
    C = ctypes
    class Address(C.Structure):
        _fields_ = [('selector', C.c_uint32), ('scope', C.c_uint32), ('element', C.c_uint32)]
    if name == 'System default microphone':
        ca = C.CDLL('/System/Library/Frameworks/CoreAudio.framework/CoreAudio')
        device = C.c_uint32()
        address = Address(int.from_bytes(b'dIn ', 'big'), int.from_bytes(b'glob', 'big'), 0)
        size = C.c_uint32(C.sizeof(device))
        status = ca.AudioObjectGetPropertyData(1, C.byref(address), 0, None,
                                               C.byref(size), C.byref(device))
        if status or not device.value:
            raise RuntimeError('No system default microphone is available')
        return device.value
    uid = mac_microphone_uid(name)
    cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    ca = C.CDLL('/System/Library/Frameworks/CoreAudio.framework/CoreAudio')
    cf.CFStringCreateWithCString.argtypes = [C.c_void_p, C.c_char_p, C.c_uint32]
    cf.CFStringCreateWithCString.restype = C.c_void_p
    cf.CFRelease.argtypes = [C.c_void_p]
    uid_ref = C.c_void_p(cf.CFStringCreateWithCString(None, uid.encode(), 0x08000100))
    if not uid_ref.value:
        raise RuntimeError('Unable to resolve microphone UID')
    try:
        device = C.c_uint32()
        address = Address(int.from_bytes(b'uidd', 'big'), int.from_bytes(b'glob', 'big'), 0)
        size = C.c_uint32(C.sizeof(device))
        status = ca.AudioObjectGetPropertyData(1, C.byref(address), C.sizeof(uid_ref),
                                               C.byref(uid_ref), C.byref(size), C.byref(device))
        if status or not device.value:
            raise RuntimeError('Selected microphone is not attached: ' + name)
        return device.value
    finally:
        cf.CFRelease(uid_ref)


class AudioEngine:
    stdin = None
    pid = None  # in-process engine, not an independently killable process

    def __init__(self, port, settings, directory, sink=None):
        self.Gst = Gst = gst_runtime()
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.rc = None
        self.created = time.monotonic()
        self.directory = pathlib.Path(directory)
        self.events = collections.deque(maxlen=50)
        self.timing = PacketTiming()
        self.capture = None
        self.discontinuities = 0
        self.qos = 0
        self.updates = 0
        self.settings = {}
        self.failure = None
        self.last_pcm = None
        sink = sink or ('osxaudiosink' if os.sys.platform == 'darwin' else 'autoaudiosink')
        if sink not in ('osxaudiosink', 'autoaudiosink', 'fakesink'):
            raise ValueError('unsupported output sink')
        pipeline = (
            'udpsrc name=source address=127.0.0.1 port=%d buffer-size=1048576 '
            'caps="application/x-rtp,media=audio,encoding-name=OPUS,payload=97,clock-rate=48000" '
            '! rtpjitterbuffer name=jitter post-drop-messages=true '
            '! rtpopusdepay ! opusdec name=decoder '
            '! audioconvert ! audioresample quality=10 '
            '! audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved '
            '! identity name=pcm '
            '! audiodynamic name=compressor mode=compressor characteristics=soft-knee threshold=0.12 '
            '! volume name=gain '
            '! audiodynamic name=limiter mode=compressor characteristics=hard-knee threshold=0.97 '
            '! audioconvert ! queue name=output_queue max-size-time=100000000 '
            'max-size-bytes=0 max-size-buffers=0 ! %s name=output sync=true' % (int(port), sink))
        if sink == 'osxaudiosink':
            pipeline += ' buffer-time=200000 latency-time=20000'
        self.pipeline = Gst.parse_launch(pipeline)
        self.elements = {n: self.pipeline.get_by_name(n) for n in
                         ('source','jitter','decoder','pcm','compressor','limiter','gain','output_queue','output')}
        self.elements['source'].get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, self._packet)
        self.elements['pcm'].get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, self._pcm)
        try:
            self.tune(settings)
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError('audio pipeline refused PLAYING')
        except Exception:
            self.pipeline.set_state(Gst.State.NULL)
            raise
        self._log_hook = self._debug_log
        Gst.debug_add_log_function(self._log_hook, None)
        Gst.debug_set_default_threshold(Gst.DebugLevel.WARNING)
        self.thread = threading.Thread(target=self._bus, name='audio-events', daemon=True)
        self.thread.start()

    def event(self, kind, detail):
        with self.lock:
            self.events.append({'at_s': round(time.monotonic()-self.created, 3),
                                'kind': kind, 'detail': str(detail)[:1200]})

    def _debug_log(self, category, level, file, function, line, obj, message, *unused):
        # Clock skew warnings are debug messages, not necessarily bus WARNINGs.
        if level > self.Gst.DebugLevel.WARNING or obj is None:
            return
        try:
            current = obj
            for _ in range(8):
                if current == self.pipeline:
                    self.event('gst-warning', message.get())
                    return
                current = current.get_parent()
                if current is None:
                    return
        except (AttributeError, TypeError):
            return

    def tune(self, settings):
        with self.lock:
            if self.rc is not None:
                raise RuntimeError('audio engine has stopped')
            s = dict(settings)
            s.setdefault("clock_mode", "slave")
            if s["clock_mode"] not in ("slave", "none"):
                raise ValueError("unsupported clock mode")
            self.elements["jitter"].set_property("mode", 1 if s["clock_mode"] == "slave" else 0)
            self.elements['gain'].set_property('volume', float(s['gain']))
            self.elements['compressor'].set_property('ratio', .1 if s['dynamics'] else 1.)
            self.elements['limiter'].set_property('ratio', .08 if s['dynamics'] else 1.)
            self.elements['jitter'].set_property('latency', int(s['jitter_ms']))
            self.elements['jitter'].set_property('do-lost', bool(s['fec'] or s['plc']))
            self.elements['decoder'].set_property('use-inband-fec', bool(s['fec']))
            self.elements['decoder'].set_property('plc', bool(s['plc']))
            self.elements['output'].set_property('sync', bool(s['sink_sync']))
            self.settings = s
            self.updates += 1
            self.pipeline.recalculate_latency()

    def _packet(self, pad, info):
        buffer = info.get_buffer()
        if buffer:
            data = buffer.extract_dup(0, buffer.get_size())
            with self.lock:
                self.timing.add(data, time.monotonic())
                if self.capture:
                    self.capture.push('rtp', data)
        return self.Gst.PadProbeReturn.OK

    def _pcm(self, pad, info):
        buffer = info.get_buffer()
        if buffer:
            with self.lock:
                self.last_pcm = time.monotonic()
                if buffer.has_flags(self.Gst.BufferFlags.DISCONT):
                    self.discontinuities += 1
                if self.capture and not self.capture.done.is_set():
                    self.capture.push('pcm', buffer.extract_dup(0, buffer.get_size()),
                                      None if buffer.pts == self.Gst.CLOCK_TIME_NONE else buffer.pts)
        return self.Gst.PadProbeReturn.OK

    def _bus(self):
        Gst = self.Gst
        bus = self.pipeline.get_bus()
        while not self.stopped.is_set():
            msg = bus.timed_pop(100*Gst.MSECOND)
            if msg is None:
                continue
            if msg.type in (Gst.MessageType.ERROR, Gst.MessageType.WARNING):
                error, debug = msg.parse_error() if msg.type == Gst.MessageType.ERROR else msg.parse_warning()
                self.event('error' if msg.type == Gst.MessageType.ERROR else 'warning',
                           '%s: %s (%s)' % (msg.src.get_name(), error.message, debug or ''))
                if msg.type == Gst.MessageType.ERROR:
                    self.failure = error.message
                    self.rc = 1
                    self.stopped.set()
            elif msg.type == Gst.MessageType.QOS:
                self.qos += 1
            elif msg.type == Gst.MessageType.LATENCY:
                self.pipeline.recalculate_latency()
            elif msg.type == Gst.MessageType.CLOCK_LOST:
                self.event('clock-lost', 'Reacquiring output clock')
                self.pipeline.set_state(Gst.State.PAUSED)
                self.pipeline.set_state(Gst.State.PLAYING)
            elif msg.type == Gst.MessageType.ELEMENT:
                structure = msg.get_structure()
                if structure:
                    self.event('element', structure.to_string())
            elif msg.type == Gst.MessageType.EOS:
                self.rc = 1
                self.failure = 'unexpected end of return stream'
                self.stopped.set()
        self.pipeline.set_state(Gst.State.NULL)
        Gst.debug_remove_log_function(self._log_hook)
        if self.capture:
            self.capture.stop()

    def snapshot(self):
        with self.lock:
            stats = self.elements['jitter'].get_property('stats')
            counters = {}
            if stats:
                for name in ('num-pushed','num-lost','num-late','num-duplicates','avg-jitter'):
                    if stats.has_field(name):
                        counters[name] = stats.get_value(name)
            clock = self.pipeline.get_clock()
            return {'backend': 'gstreamer-persistent', 'running': self.rc is None,
                    'uptime_s': round(time.monotonic()-self.created, 2),
                    'clock': clock.get_name() if clock else None,
                    'settings': dict(self.settings), 'property_updates': self.updates,
                    'packets': self.timing.snapshot(), 'jitter': counters,
                    'output_queue_ms': self.elements['output_queue'].get_property('current-level-time')/1e6,
                    'pcm_discontinuities': self.discontinuities, 'qos_messages': self.qos,
                    'last_pcm_age_ms': (time.monotonic()-self.last_pcm)*1000 if self.last_pcm else None,
                    'failure': self.failure, 'events': list(self.events),
                    'capture': self.capture.snapshot() if self.capture else None}

    def start_capture(self, seconds):
        with self.lock:
            if self.rc is not None:
                raise RuntimeError('audio engine is not running')
            if self.capture and not self.capture.done.is_set():
                raise ValueError('a capture is already active')
            self.capture = Capture(self.directory / 'captures', float(seconds),
                                   {'engine': 'gstreamer-persistent', 'settings': dict(self.settings),
                                    'gstreamer': self.Gst.version_string()})
            return self.capture.snapshot()

    def poll(self):
        return self.rc

    def terminate(self):
        self.rc = 0 if self.rc is None else self.rc
        self.stopped.set()
        self.thread.join(timeout=5)

    kill = terminate

    def wait(self, timeout=None):
        if not self.stopped.wait(timeout):
            raise subprocess.TimeoutExpired('audio-engine', timeout)
        self.thread.join(timeout=5)
        return self.rc
