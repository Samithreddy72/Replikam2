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
    try:
        gi.require_version('Gst', '1.0')
    except ValueError as exc:
        # GI without the Gst typelib is as unusable as no GI at all: report it the same
        # way, so the caller falls back to the legacy player instead of playing nothing.
        raise ImportError(str(exc)) from exc
    from gi.repository import Gst
    Gst.init(None)
    return Gst


# ECHO CANCELLATION, AND WHY IT HAS TO HAPPEN IN THIS PROCESS.
#
# Meeting audio plays out of the presenter's speakers, their microphone hears it, and it goes
# back to the meeting: remote participants hear themselves. The meeting app's own canceller
# cannot remove it - by the time it returns, it has crossed the mesh twice and two jitter
# buffers (roughly half a second, and never the same half second). Only this machine sees
# both signals a few milliseconds apart: what the speakers play and what the mic captures.
#
# GStreamer's webrtcdsp subtracts what a webrtcechoprobe recorded, but it finds that probe by
# name in a process-wide list - so the return player and the microphone must share a process.
# That is why the microphone moves in here when echo cancellation is available.
ECHO_PROBE = 'netbridge_echo_probe'


def echo_cancellation_available():
    try:
        Gst = gst_runtime()
    except Exception:
        return False
    return all(Gst.ElementFactory.find(name) for name in ('webrtcdsp', 'webrtcechoprobe'))


def microphone_source(Gst, name):
    """The capture element for the selected microphone, resolved in THIS process."""
    factory = 'osxaudiosrc' if os.sys.platform == 'darwin' else 'wasapi2src'
    if not name or name == 'System default microphone':
        element = Gst.ElementFactory.make(factory, 'microphone')
        if element is None:
            raise RuntimeError('%s is not available' % factory)
        return element
    monitor = Gst.DeviceMonitor()
    monitor.add_filter('Audio/Source', None)
    if not monitor.start():
        raise RuntimeError('Microphone discovery failed')
    try:
        devices = [d for d in monitor.get_devices() if d.get_display_name() == name]
    finally:
        monitor.stop()
    def api(device):
        properties = device.get_properties()
        return properties.get_string('device.api') if properties else None
    # Windows lists one input through several providers; prefer the one the
    # default factory belongs to.
    devices.sort(key=lambda device: 0 if api(device) in ('wasapi2', 'osxaudio') else 1)
    if not devices:
        raise RuntimeError('Microphone not found: %s' % name)
    element = devices[0].create_element('microphone')
    if element is None:
        raise RuntimeError('Microphone could not be opened: %s' % name)
    return element


class MicEngine:
    """Presenter microphone -> Opus/RTP to the bridge, in-process, optionally echo-cancelled.

    Sends exactly what the CoreAudio leg sent (48 kHz stereo Opus, 64 kbit/s, 20 ms frames,
    PT 97), so the bridge cannot tell the difference. Same process-like surface as
    AudioEngine, so the session supervises it as just another leg.
    """
    in_process = True
    stdin = None
    pid = None

    def __init__(self, name, host, port, gain_db=0, echo_probe=None, source=None):
        self.Gst = Gst = gst_runtime()
        self.rc = None
        self.failure = None
        self.stopped = threading.Event()
        self.created = time.monotonic()
        self.name = name
        self.echo_probe = echo_probe
        # Mono into the canceller (a microphone is mono anyway, and it halves the work).
        # Gain control and noise suppression stay off: the bridge owns voice AGC.
        aec = ('webrtcdsp name=aec probe=%s echo-cancel=true gain-control=false '
               'noise-suppression=false ! ' % echo_probe) if echo_probe else ''
        gain = 'volume volume=%s ! ' % (10 ** (float(gain_db) / 20)) if gain_db else ''
        chain = ('audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=1 ! '
                 + aec + 'audioconvert ! audio/x-raw,channels=2 ! ' + gain +
                 'audioconvert ! audio/x-raw,format=S16LE ! '
                 'opusenc bitrate=64000 audio-type=voice frame-size=20 inband-fec=true '
                 'packet-loss-percentage=5 ! rtpopuspay pt=97 ! '
                 'udpsink name=sender host=%s port=%d sync=false async=false' % (json.dumps(host), int(port)))
        if source is not None:      # tests substitute a generator for the physical device
            self.pipeline = Gst.parse_launch(source + ' ! ' + chain)
        else:
            self.pipeline = Gst.Pipeline.new('netbridge_presenter_microphone')
            microphone = microphone_source(Gst, name)
            rest = Gst.parse_bin_from_description(chain, True)
            self.pipeline.add(microphone)
            self.pipeline.add(rest)
            if not microphone.link(rest):
                raise RuntimeError('Microphone could not be linked: %s' % name)
        bus = self.pipeline.get_bus()
        sending = threading.Event()
        def first_packet(pad, info):
            sending.set()
            return Gst.PadProbeReturn.REMOVE
        self.pipeline.get_by_name('sender').get_static_pad('sink').add_probe(
            Gst.PadProbeType.BUFFER, first_packet)
        # Fail HERE, not later on the bus: a busy device, or a canceller that cannot find its
        # probe, must raise to the caller so it can fall back to the subprocess microphone.
        # An error after PLAYING would only reach the supervisor, which retries the same
        # failure until it gives up - a session with no voice. So wait for audio to leave.
        failed = self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE
        deadline = time.monotonic() + 3
        while not failed and not sending.is_set() and time.monotonic() < deadline:
            message = bus.timed_pop_filtered(50 * Gst.MSECOND, Gst.MessageType.ERROR)
            if message is not None:
                failed = message.parse_error()[0].message or 'Microphone pipeline failed'
        if failed:
            if failed is True:
                message = bus.pop_filtered(Gst.MessageType.ERROR)
                failed = message.parse_error()[0].message if message else 'Microphone pipeline refused PLAYING'
            self.pipeline.set_state(Gst.State.NULL)
            raise RuntimeError(failed)
        self.thread = threading.Thread(target=self._bus, name='microphone-events', daemon=True)
        self.thread.start()

    def _bus(self):
        Gst = self.Gst
        bus = self.pipeline.get_bus()
        while not self.stopped.is_set():
            msg = bus.timed_pop_filtered(100 * Gst.MSECOND,
                                         Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is None:
                continue
            self.failure = (msg.parse_error()[0].message if msg.type == Gst.MessageType.ERROR
                            else 'microphone stream ended')
            self.rc = 1
            self.stopped.set()
        # NULL is what releases the capture device.
        self.pipeline.set_state(Gst.State.NULL)

    def snapshot(self):
        return {'backend': 'gstreamer-inprocess', 'running': self.rc is None,
                'device': self.name, 'echo_cancellation': bool(self.echo_probe),
                'uptime_s': round(time.monotonic() - self.created, 2), 'failure': self.failure}

    def poll(self):
        return self.rc

    def terminate(self):
        self.rc = 0 if self.rc is None else self.rc
        self.stopped.set()
        self.thread.join(timeout=5)

    kill = terminate

    def wait(self, timeout=None):
        if not self.stopped.wait(timeout):
            raise subprocess.TimeoutExpired('microphone', timeout)
        self.thread.join(timeout=5)
        return self.rc


def selftest(directory, timeout=5.0):
    """Prove the persistent receiver works in THIS process: GI, typelibs and plugins.

    A frozen app cannot tell from outside whether its bundled GI runtime loads, so the
    release build runs this through the packaged engine. fakesink keeps it off the sound
    device (CI runners have none); the real sinks are only checked to exist.
    """
    import socket
    import struct
    Gst = gst_runtime()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    settings = dict(gain=1., jitter_ms=80, dynamics=False, sink_sync=True, fec=True, plc=False)
    engine = AudioEngine(port, settings, directory, sink='fakesink')
    try:
        deadline = time.monotonic() + timeout
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            seq = 0
            while engine.snapshot()['packets']['packets'] == 0 and time.monotonic() < deadline:
                # RTP/Opus PT 97 carrying a valid 20 ms Opus silence frame.
                sender.sendto(struct.pack('!BBHII', 0x80, 97, seq, seq * 960, 1) + b'\xf8\xff\xfe',
                              ('127.0.0.1', port))
                seq += 1
                time.sleep(.02)
        snapshot = engine.snapshot()
    finally:
        engine.terminate()
    def present(*names):
        return {name: Gst.ElementFactory.find(name) is not None for name in names}
    sinks = present('autoaudiosink', *{
        'darwin': ('osxaudiosink',), 'win32': ('wasapi2sink', 'wasapisink', 'directsoundsink'),
    }.get(os.sys.platform, ('pulsesink', 'alsasink')))
    packets = snapshot['packets']['packets']
    ok = packets > 0 and snapshot['running'] and sinks['autoaudiosink'] and any(
        found for name, found in sinks.items() if name != 'autoaudiosink')
    return {'ok': bool(ok), 'gstreamer': Gst.version_string(), 'packets': packets,
            'failure': snapshot['failure'], 'sinks': sinks,
            # Not required yet; reported so echo-cancellation work knows what ships.
            'echo_cancellation': present('webrtcdsp', 'webrtcechoprobe')}


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
            'name=netbridge_presenter_microphone',
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
    in_process = True
    stdin = None
    pid = None  # in-process engine, not an independently killable process

    def __init__(self, port, settings, directory, sink=None, echo_probe=True):
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
        # The echo canceller's reference: what actually reaches the speaker, recorded as
        # late as possible. A passthrough; without webrtcdsp bundled it is simply left out.
        self.echo_probe = ECHO_PROBE if echo_probe and Gst.ElementFactory.find('webrtcechoprobe') else None
        reference = (('! audio/x-raw,format=S16LE,layout=interleaved ', 'webrtcechoprobe name=%s ! ' % ECHO_PROBE)
                     if self.echo_probe else ('', ''))
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
            '! audioconvert %s! queue name=output_queue max-size-time=100000000 '
            'max-size-bytes=0 max-size-buffers=0 ! %s%s name=output sync=true'
            % (int(port), reference[0], reference[1], sink))
        if sink == 'osxaudiosink':
            pipeline += ' buffer-time=200000 latency-time=20000'
        self.pipeline = Gst.parse_launch(pipeline)
        self.elements = {n: self.pipeline.get_by_name(n) for n in
                         ('source','jitter','decoder','pcm','compressor','limiter','gain','output_queue','output')}
        # What _debug_log may attribute to this pipeline, known without asking GStreamer:
        # these elements, and any added later (autoaudiosink adds its real sink at start).
        self._own = {self.pipeline, *self.elements.values()}
        self.pipeline.connect('deep-element-added', lambda _bin, _sub, element: self._own.add(element))
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
        #
        # This hook is process-wide and runs in whichever thread logged - often while
        # GStreamer holds a lock on `obj`. It must never call back into GStreamer: walking
        # get_parent() here took that same lock and froze the whole engine (a failing
        # microphone pipeline did it every time). Match against objects collected up front,
        # and append without self.lock (deque.append is atomic), so it never waits on anyone.
        if level > self.Gst.DebugLevel.WARNING or obj is None:
            return
        try:
            if obj not in self._own:
                return
        except TypeError:
            return
        self.events.append({'at_s': round(time.monotonic()-self.created, 3),
                            'kind': 'gst-warning', 'detail': str(message.get())[:1200]})

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
