#!/usr/bin/env python3
"""Replay a diagnostic trace through GStreamer and an optional upstream NetEq tool.

No live meeting, bridge or mesh changes. Requires the GI development interpreter.
NetEq is not emulated: without --neteq the report explicitly says it was not run.
"""
import argparse
import hashlib
import json
import pathlib
import shutil
import socket
import struct
import subprocess
import sys
import time
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/netbridge-source'))
from audio_engine import AudioEngine


def packets(path):
    with open(path, 'rb') as f:
        if not f.readline(256).startswith(b'#!rtpplay1.0 '):
            raise ValueError('not an rtpplay1.0 dump')
        if len(f.read(16)) != 16:
            raise ValueError('truncated dump header')
        previous = 0
        while True:
            header = f.read(8)
            if not header:
                return
            if len(header) != 8:
                raise ValueError('truncated packet header')
            size, plen, ms = struct.unpack('!HHI', header)
            if size < 8 or plen != size-8 or plen > 65527 or ms < previous:
                raise ValueError('invalid packet record')
            data = f.read(plen)
            if len(data) != plen:
                raise ValueError('truncated RTP payload')
            previous = ms
            yield ms/1000, data


def wav_info(path):
    with wave.open(str(path), 'rb') as w:
        return {'rate': w.getframerate(), 'channels': w.getnchannels(),
                'seconds': w.getnframes()/w.getframerate()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=pathlib.Path)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    parser.add_argument('--neteq', type=pathlib.Path, help='upstream neteq_rtpplay executable')
    parser.add_argument('--jitter-ms', type=int, default=150)
    parser.add_argument('--clock-mode', choices=('slave', 'none'), default='slave',
                        help='offline jitter-buffer clock experiment; none disables skew adjustment')
    args = parser.parse_args()
    if args.trace.stat().st_size > 24*1024*1024:
        parser.error('trace exceeds 24 MiB')
    records = list(packets(args.trace))
    if not records or records[-1][0] > 58:
        parser.error('use a nonempty trace of at most 58 seconds')
    args.output.mkdir(parents=True, exist_ok=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    settings = dict(gain=1., jitter_ms=max(0,min(1000,args.jitter_ms)),
                    dynamics=False,sink_sync=True,fec=True,plc=False,
                    clock_mode=args.clock_mode)
    engine = AudioEngine(port, settings, args.output, sink='fakesink')
    capture = engine.start_capture(min(60,records[-1][0]+2))
    schedule_errors = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            start = time.monotonic()
            for elapsed, data in records:
                time.sleep(max(0, start+elapsed-time.monotonic()))
                schedule_errors.append((time.monotonic()-start-elapsed)*1000)
                sender.sendto(data, ('127.0.0.1', port))
            time.sleep(settings['jitter_ms']/1000+.5)
        baseline = engine.snapshot()
    finally:
        engine.terminate()
    baseline['capture'] = engine.capture.snapshot()
    if baseline.get('failure') or not baseline['capture']['valid']:
        raise RuntimeError('GStreamer replay did not produce a valid capture: %s' % baseline)
    directory = pathlib.Path(capture['directory'])
    baseline_wav = args.output / 'gstreamer.wav'
    shutil.copyfile(directory/'decoded.wav', baseline_wav)
    report = {'trace_sha256':hashlib.sha256(args.trace.read_bytes()).hexdigest(),
              'packets':len(records), 'baseline':baseline, 'clock_mode':args.clock_mode,
              'gstreamer_audio':wav_info(baseline_wav),
              'replay_scheduling_max_error_ms':max(schedule_errors),
              'neteq':{'status':'not_run','reason':'No upstream neteq_rtpplay binary supplied'},
              'interpretation':'Listening comparison and delay logs, not an objective speech-quality score. PCM duration alone is not end-to-end latency.'}
    if args.neteq:
        binary = args.neteq.resolve()
        cmd = [str(binary), '--opus=97', '--textlog',
               '--output_files_base_name='+str((args.output/'neteq').resolve()),
               str(args.trace.resolve()), str((args.output/'neteq.wav').resolve())]
        try:
            with (args.output/'neteq.log').open('w') as log:
                result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=120)
            report['neteq'] = {'status':'completed' if result.returncode==0 else 'failed',
                              'exit_code':result.returncode,
                              'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
                              'command':cmd}
            if result.returncode==0:
                report['neteq']['audio']=wav_info(args.output/'neteq.wav')
        except (OSError,subprocess.TimeoutExpired,wave.Error) as exc:
            report['neteq']={'status':'failed','reason':str(exc)}
    (args.output/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
    return 0 if report['neteq']['status']=='completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
