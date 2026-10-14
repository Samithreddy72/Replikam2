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


# ECHO CANCELLATION, AND WHY THE MICROPHONE JOINS THE RETURN PLAYER'S PIPELINE.
#
# Meeting audio plays out of the presenter's speakers, their microphone hears it, and it goes
# back to the meeting: remote participants hear themselves. The meeting app's own canceller
# cannot remove it - by the time it returns, it has crossed the mesh twice and two jitter
# buffers (roughly half a second, and never the same half second). Only this machine sees
# both signals a few milliseconds apart: what the speakers play and what the mic captures.
#
# webrtcdsp cancels against what a webrtcechoprobe recorded, and asks it for "the far-end audio
# played N ms after this capture" by comparing RUNNING TIMES. A running time only means the same
# thing inside one pipeline (one clock, one base time); webrtcdsp's documentation says "the probe
# can only be used within the same top level GstPipeline". 0.0.104 ran the microphone as its own
# pipeline: the probe was found (lookup is by name, process-wide), the canceller ran, and it was
# handed silence or misaligned audio - so on a Mac with speakers the room still heard itself.
# So the microphone runs INSIDE the return player's pipeline (AudioEngine.attach_microphone),
# as an independent branch whose failures never take meeting audio down. Without meeting audio
# playing here there is nothing to cancel, and the microphone runs alone (MicEngine).
ECHO_PROBE = 'netbridge_echo_probe'
# The reference branch (AudioEngine._reference). Missing any of them must cost only echo
# cancellation, never meeting audio: an unparseable receiver plays nothing at all.
REFERENCE_ELEMENTS = ('webrtcechoprobe', 'tee', 'appsink', 'appsrc')


def echo_cancellation_available():
    try:
        Gst = gst_runtime()
    except Exception:
        return False
    return all(Gst.ElementFactory.find(name) for name in ('webrtcdsp',) + REFERENCE_ELEMENTS)


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


def microphone_bin(Gst, name, host, port, gain_db=0, echo_probe=None, source=None):
    """Capture -> [echo cancellation] -> exactly what the CoreAudio leg sent (48 kHz stereo
    Opus, 64 kbit/s, 20 ms frames, PT 97), so the bridge cannot tell the difference.

    Mono into the canceller (a microphone is mono anyway, and it halves the work). Gain control
    and noise suppression stay off: the bridge owns voice AGC. `source` lets tests substitute a
    generator for the physical device. Returns a self-contained bin ending in udpsink 'sender'.
    """
    aec = ('webrtcdsp name=aec probe=%s echo-cancel=true gain-control=false '
           'noise-suppression=false ! ' % echo_probe) if echo_probe else ''
    gain = 'volume volume=%s ! ' % (10 ** (float(gain_db) / 20)) if gain_db else ''
    chain = ('audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=1 ! '
             + aec + 'audioconvert ! audio/x-raw,channels=2 ! ' + gain +
             'audioconvert ! audio/x-raw,format=S16LE ! '
             'opusenc bitrate=64000 audio-type=voice frame-size=20 inband-fec=true '
             'packet-loss-percentage=5 ! rtpopuspay pt=97 ! '
             'udpsink name=sender host=%s port=%d sync=false async=false' % (json.dumps(host), int(port)))
    if source is not None:
        return Gst.parse_bin_from_description(source + ' ! ' + chain, False)
    branch = Gst.Bin.new(None)
    microphone = microphone_source(Gst, name)
    rest = Gst.parse_bin_from_description(chain, True)
    branch.add(microphone)
    branch.add(rest)
    if not microphone.link(rest):
        raise RuntimeError('Microphone could not be linked: %s' % name)
    return branch


def _first_packet(Gst, branch):
    """An Event set when the microphone's first RTP packet reaches its udpsink."""
    sending = threading.Event()
    def seen(pad, info):
        sending.set()
        return Gst.PadProbeReturn.REMOVE
    branch.get_by_name('sender').get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, seen)
    return sending


class MicEngine:
    """The microphone on its own: used when no meeting audio plays here (nothing to cancel).

    Same process-like surface as AudioEngine, so the session supervises it as just another leg.
    """
    in_process = True
    stdin = None
    pid = None
    echo_probe = None

    def __init__(self, name, host, port, gain_db=0, source=None):
        self.Gst = Gst = gst_runtime()
        self.rc = None
        self.failure = None
        self.stopped = threading.Event()
        self.created = time.monotonic()
        self.name = name
        self.pipeline = Gst.Pipeline.new('netbridge_presenter_microphone')
        branch = microphone_bin(Gst, name, host, port, gain_db, source=source)
        self.pipeline.add(branch)
        bus = self.pipeline.get_bus()
        sending = _first_packet(Gst, branch)
        # Fail HERE, not later on the bus: a busy device must raise to the caller so it can
        # fall back to the subprocess microphone. An error after PLAYING would only reach the
        # supervisor, which retries the same failure until it gives up - a session with no
        # voice. So wait for audio to leave (or an error).
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
                'device': self.name, 'echo_cancellation': False,
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


class AttachedMicrophone:
    """The microphone as a branch of the return player's pipeline, echo-cancelled against its
    probe in the same running time. Created by AudioEngine.attach_microphone.

    Its errors are routed here by the engine's bus thread and remove only this branch; meeting
    audio keeps playing. If the engine stops, this reports dead and the supervisor restarts it.
    """
    in_process = True
    stdin = None
    pid = None

    def __init__(self, engine, name, host, port, gain_db=0, source=None):
        Gst = engine.Gst
        self.engine = engine
        self.name = name
        self.echo_probe = engine.echo_probe
        self.rc = None
        self.failure = None
        self.stopped = threading.Event()
        self.created = time.monotonic()
        self._detached = False
        self._lock = threading.Lock()
        self.branch = microphone_bin(Gst, name, host, port, gain_db, self.echo_probe, source)
        sending = _first_packet(Gst, self.branch)
        engine._adopt_microphone(self)
        if not self.branch.sync_state_with_parent():
            self._fail('Microphone could not start')
        # Same contract as MicEngine: fail to the caller, not later to the supervisor.
        deadline = time.monotonic() + 3
        while not sending.is_set() and not self.stopped.is_set() and time.monotonic() < deadline:
            time.sleep(.02)
        if self.stopped.is_set():
            self.detach()
            raise RuntimeError(self.failure or 'Microphone could not start')

    def _fail(self, message):
        # Called from the engine's bus thread (or from __init__): mark first, then tear down.
        if self.rc is None:
            self.failure = message
            self.rc = 1
        self.stopped.set()

    def _ended(self, message):
        """The engine stopped, taking this branch with it."""
        if self.rc is None:
            self.failure = message
            self.rc = 1
        self._detached = True
        self.stopped.set()

    def detach(self):
        with self._lock:     # the session and the engine's bus thread may both get here
            if self._detached:
                return
            self._detached = True
        Gst = self.engine.Gst
        # NULL releases the capture device and the canceller's claim on the probe.
        self.branch.set_state(Gst.State.NULL)
        self.engine._release_microphone(self)

    def snapshot(self):
        return {'backend': 'gstreamer-inprocess', 'running': self.rc is None,
                'device': self.name, 'echo_cancellation': True,
                'uptime_s': round(time.monotonic() - self.created, 2), 'failure': self.failure}

    def poll(self):
        return self.rc

    def terminate(self):
        self.rc = 0 if self.rc is None else self.rc
        self.stopped.set()
        self.detach()

    kill = terminate

    def wait(self, timeout=None):
        if not self.stopped.wait(timeout):
            raise subprocess.TimeoutExpired('microphone', timeout)
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

    def __init__(self, port, settings, directory, sink=None, echo_probe=True, device_caps=None):
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
        self.microphone = None      # an AttachedMicrophone branch, while echo-cancelling
        sink = sink or ('osxaudiosink' if os.sys.platform == 'darwin' else 'autoaudiosink')
        if sink not in ('osxaudiosink', 'autoaudiosink', 'fakesink'):
            raise ValueError('unsupported output sink')
        # The echo canceller's reference: the audio about to reach the speaker, on a side branch
        # of its own (see _reference). Left out when webrtcdsp is not bundled.
        self.echo_probe = ECHO_PROBE if echo_probe and all(
            Gst.ElementFactory.find(name) for name in REFERENCE_ELEMENTS) else None
        # Tests emulate a device that accepts only its own format (fakesink takes anything).
        device = 'capsfilter caps="%s" ! ' % device_caps if device_caps else ''
        output = sink + (' buffer-time=200000 latency-time=20000' if sink == 'osxaudiosink' else '')
        if self.echo_probe:
            # S16LE for the probe; the output branch converts AGAIN, because the device decides
            # its format: Windows' WASAPI takes only its mix format (usually 32-bit float), and
            # 0.0.104 forced S16LE into it - no meeting audio at all on Windows.
            split = ('! audio/x-raw,format=S16LE,layout=interleaved ! tee name=far ',
                     'audioconvert ! audioresample ! ')
            reference = (
                ' far. ! queue leaky=downstream max-size-time=200000000 max-size-bytes=0 max-size-buffers=0 '
                '! appsink name=reference_tap sync=false emit-signals=true max-buffers=50 drop=true '
                'appsrc name=reference_feed is-live=true format=time min-latency=0 '
                '! webrtcechoprobe name=%s ! fakesink sync=false async=false' % ECHO_PROBE)
        else:
            split, reference = ('', ''), ''
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
            'max-size-bytes=0 max-size-buffers=0 ! %s%s%s name=output sync=true%s'
            % (int(port), split[0], split[1], device, output, reference))
        self.pipeline = Gst.parse_launch(pipeline)
        self.elements = {n: self.pipeline.get_by_name(n) for n in
                         ('source','jitter','decoder','pcm','compressor','limiter','gain','output_queue','output')}
        # What _debug_log may attribute to this pipeline, known without asking GStreamer:
        # these elements, and any added later (autoaudiosink adds its real sink at start).
        self._own = {self.pipeline, *self.elements.values()}
        self.pipeline.connect('deep-element-added', lambda _bin, _sub, element: self._own.add(element))
        self.elements['source'].get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, self._packet)
        self.elements['pcm'].get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, self._pcm)
        if self.echo_probe:
            self._reference_feed = self.pipeline.get_by_name('reference_feed')
            self._reference_caps = None
            self.pipeline.get_by_name('reference_tap').connect('new-sample', self._reference)
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

    def _reference(self, tap):
        """Hand the echo probe what is about to play, behind a zero-latency live source.

        For a capture stamped t, webrtcechoprobe returns the far-end audio that plays at
        t + delay, where delay is the latency UPSTREAM of the probe. Inline before the sink
        that is the jitter buffer and Opus FEC - 200 ms or more - but meeting audio reaches
        that point only a few ms before it plays, so audio for t + 200 ms is never there yet
        and the probe hands the canceller silence: it cancelled nothing (measured on GStreamer
        1.20 and 1.28 alike). Behind appsrc min-latency=0 the delay is 0: the probe hands
        what plays at t, which is always buffered, and the canceller finds the remaining
        speaker-to-microphone delay itself. Buffers keep their timestamps, and the probe still
        gets the pipeline's real playout latency, so the alignment is exact.
        """
        sample = tap.emit('pull-sample')
        if sample is None:
            return self.Gst.FlowReturn.EOS
        caps = sample.get_caps()
        if caps is not None and caps != self._reference_caps:
            self._reference_feed.set_property('caps', caps)
            self._reference_caps = caps
        self._reference_feed.emit('push-buffer', sample.get_buffer())
        return self.Gst.FlowReturn.OK

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
            self.elements['output'].set_property('sync', self._output_sync(s))
            self.settings = s
            self.updates += 1
            self.pipeline.recalculate_latency()

    def _output_sync(self, settings):
        # The probe learns when audio really plays from the LATENCY event, which only a sink
        # that syncs to the clock sends. Unsynced (the Windows default), it feeds the canceller
        # silence. So while a canceller is attached the output syncs, whatever the setting.
        return bool(settings.get('sink_sync')) or self.microphone is not None

    def attach_microphone(self, name, host, port, gain_db=0, source=None):
        """Run the microphone as a branch of THIS pipeline, echo-cancelled against its probe
        (see ECHO_PROBE for why it must be the same pipeline). Raises if it cannot start."""
        if not self.echo_probe:
            raise RuntimeError('echo cancellation is not available in this player')
        if self.rc is not None:
            raise RuntimeError('the meeting audio player has stopped')
        current = self.microphone
        if current is not None and current.poll() is None:
            raise RuntimeError('a microphone is already attached')
        return AttachedMicrophone(self, name, host, port, gain_db, source)

    def _adopt_microphone(self, microphone):
        self.microphone = microphone
        self.pipeline.add(microphone.branch)
        self.elements['output'].set_property('sync', self._output_sync(self.settings))
        self.pipeline.recalculate_latency()

    def _release_microphone(self, microphone):
        try:
            self.pipeline.remove(microphone.branch)
        except Exception:
            pass
        if self.microphone is microphone:
            self.microphone = None
            if self.rc is None:
                self.elements['output'].set_property('sync', self._output_sync(self.settings))
                self.pipeline.recalculate_latency()

    def _from_microphone(self, msg):
        microphone = self.microphone
        source = msg.src
        # The bus thread holds no GStreamer locks, so walking ancestors is safe here.
        return (microphone is not None and source is not None
                and (source == microphone.branch or source.has_as_ancestor(microphone.branch)))

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
                if msg.type == Gst.MessageType.ERROR and self._from_microphone(msg):
                    # Only the microphone branch failed: drop it, keep meeting audio playing.
                    # The supervisor sees the voice leg dead and starts it again.
                    microphone = self.microphone
                    self.event('microphone-error', '%s: %s' % (msg.src.get_name(), error.message))
                    microphone._fail(error.message)
                    microphone.detach()
                    continue
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
        microphone = self.microphone
        if microphone is not None:
            microphone._ended('the meeting audio player stopped')
        self.pipeline.set_state(Gst.State.NULL)
        self.microphone = None
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
                    'microphone': self.microphone.snapshot() if self.microphone else None,
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
