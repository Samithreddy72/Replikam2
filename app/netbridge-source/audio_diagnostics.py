"""Bounded, opt-in audio capture. Disk I/O never runs on a media streaming thread."""
import collections
import json
import pathlib
import queue
import struct
import threading
import time
import uuid
import wave


class PacketTiming:
    def __init__(self):
        self.count = 0
        self.invalid = 0
        self.last = None
        self.gaps = collections.deque(maxlen=3000)
        self.sequence_gaps = 0
        self.reordered = 0
        self.ssrc_changes = 0
        self.timestamp_jumps = 0

    def add(self, data, now):
        if len(data) < 12 or data[0] >> 6 != 2:
            self.invalid += 1
            return
        seq, stamp, ssrc = struct.unpack_from('!HII', data, 2)
        self.count += 1
        if self.last:
            prev_time, prev_seq, prev_stamp, prev_ssrc = self.last
            self.gaps.append(max(0, (now-prev_time)*1000))
            if ssrc != prev_ssrc:
                self.ssrc_changes += 1
            else:
                step = (seq-prev_seq+32768) % 65536-32768
                if step > 1:
                    self.sequence_gaps += step-1
                elif step <= 0:
                    self.reordered += 1
                delta = (stamp-prev_stamp+2**31) % 2**32-2**31
                if step > 0 and (delta <= 0 or delta > 48000):
                    self.timestamp_jumps += 1
        self.last = (now, seq, stamp, ssrc)

    def snapshot(self):
        gaps = sorted(self.gaps)
        return {'packets': self.count, 'invalid_packets': self.invalid,
                # Forward gaps can be repaired by later reordering: NOT confirmed loss.
                'sequence_gap_observations': self.sequence_gaps,
                'reordered_or_duplicate': self.reordered, 'ssrc_changes': self.ssrc_changes,
                'timestamp_jump_observations': self.timestamp_jumps,
                'arrival_gap_p99_ms': gaps[int((len(gaps)-1)*.99)] if gaps else None,
                'arrival_gap_max_ms': gaps[-1] if gaps else None,
                'last_packet_age_ms': (time.monotonic()-self.last[0])*1000 if self.last else None}


class Capture:
    MAX_BYTES = 24 * 1024 * 1024

    def __init__(self, directory, seconds, metadata):
        if not 1 <= seconds <= 60:
            raise ValueError('capture duration must be 1–60 seconds')
        self.id = uuid.uuid4().hex
        self.directory = pathlib.Path(directory) / self.id
        self.directory.mkdir(mode=0o700, parents=True)
        self.seconds = seconds
        self.start = time.monotonic()
        self.deadline = self.start + seconds
        self.metadata = metadata
        self.queue = queue.Queue(maxsize=256)
        self.stop_event = threading.Event()
        self.done = threading.Event()
        self.dropped = 0
        self.bytes = 0
        self.rtp_packets = 0
        self.pcm_bytes = 0
        self.error = None
        self.thread = threading.Thread(target=self._write, name='audio-capture', daemon=True)
        self.thread.start()

    def push(self, kind, data, pts=None):
        now = time.monotonic()
        if self.stop_event.is_set() or self.done.is_set() or now >= self.deadline:
            return
        try:
            self.queue.put_nowait((kind, now-self.start, data, pts))
        except queue.Full:
            self.dropped += 1

    def snapshot(self):
        return {'id': self.id, 'active': not self.done.is_set(), 'seconds': self.seconds,
                'directory': str(self.directory), 'bytes': self.bytes,
                'dropped_capture_buffers': self.dropped, 'error': self.error,
                'rtp_packets': self.rtp_packets, 'pcm_bytes': self.pcm_bytes,
                'valid': self.done.is_set() and not self.error and self.dropped == 0 and self.rtp_packets > 0 and self.pcm_bytes > 0}

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)

    def _write(self):
        try:
            with (self.directory / 'input.rtpdump').open('wb') as rtp, \
                    wave.open(str(self.directory / 'decoded.wav'), 'wb') as pcm, \
                    (self.directory / 'timing.jsonl').open('w') as timing:
                pcm.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
                # Standard rtpplay header, network byte order; contains real Opus payloads.
                rtp.write(b'#!rtpplay1.0 127.0.0.1/5004\n')
                wall = time.time()
                rtp.write(struct.pack('!IIIHH', int(wall), int(wall % 1*1e6), 0, 5004, 0))
                while True:
                    if time.monotonic() >= self.deadline:
                        self.stop_event.set()
                    try:
                        kind, elapsed, data, pts = self.queue.get(timeout=.1)
                    except queue.Empty:
                        if self.stop_event.is_set():
                            break
                        continue
                    if self.bytes + len(data) > self.MAX_BYTES:
                        self.error = 'capture byte limit reached'
                        self.stop_event.set()
                        break
                    if kind == 'rtp':
                        if len(data) > 65527:
                            self.dropped += 1
                            continue
                        rtp.write(struct.pack('!HHI', len(data)+8, len(data), int(elapsed*1000)))
                        rtp.write(data)
                        self.rtp_packets += 1
                    else:
                        pcm.writeframesraw(data)
                        self.pcm_bytes += len(data)
                    timing.write(json.dumps({'kind': kind, 'elapsed_s': elapsed,
                                             'bytes': len(data), 'pts_ns': pts})+'\n')
                    self.bytes += len(data)
        except Exception as exc:
            self.error = str(exc)
        finally:
            try:
                completed = self.snapshot()
                completed['active'] = False
                completed['valid'] = not self.error and self.dropped == 0 and self.rtp_packets > 0 and self.pcm_bytes > 0
                (self.directory / 'manifest.json').write_text(json.dumps(
                    dict(self.metadata, capture=completed,
                         format='rtpplay1.0', payload_type=97, rate=48000, channels=2), indent=2))
            except Exception as exc:
                self.error = str(exc)
            self.done.set()
