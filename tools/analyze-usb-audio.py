#!/usr/bin/env python3
"""Measure USB-packet-aligned discontinuities and stale-sample recurrence in PCM WAV.
These are diagnostic signatures, not a perceptual quality score. Periodic test tones,
repeated source material and silence can confound recurrence; compare controlled captures.
Requires numpy. At 48 kHz/1 ms USB intervals with 32 queued requests, the recycle lag is
1536 frames. Operates separately per channel and excludes near-silence from equality.
"""
import argparse,json,wave
import numpy as np

def analyze(path, requests=32):
    with wave.open(str(path),'rb') as w:
        if w.getsampwidth()!=2: raise ValueError('requires signed 16-bit PCM WAV')
        rate, channels=w.getframerate(),w.getnchannels()
        x=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').reshape(-1,channels).astype(np.int32)
    if requests < 1 or rate < 1000:
        raise ValueError("positive request count and sample rate >=1000 required")
    packet=rate//1000
    lag=packet*requests
    if len(x) <= lag + packet:
        raise ValueError("capture is too short for the configured recycle lag")
    result={'file':str(path),'rate':rate,'channels':channels,'seconds':len(x)/rate,
            'assumed_usb_interval_ms':1,'requests':requests,'channels_analysis':[]}
    for c in range(channels):
        a=x[:,c]; d=np.diff(a).astype(float)
        energy=np.array([np.mean(d[i::packet]**2) for i in range(packet)])
        eligible=(np.abs(a[lag:])>32)&(np.abs(a[:-lag])>32)
        match=(a[lag:]==a[:-lag])&eligible
        result['channels_analysis'].append({'peak':int(np.max(np.abs(a))),
            'rms_dbfs':float(20*np.log10(max(1,np.sqrt(np.mean(a.astype(float)**2)))/32768)),
            'clipped_samples':int(np.sum(np.abs(a)>=32767)),
            'eligible_recurrence_samples':int(np.sum(eligible)),
            'recycle_exact_match_percent':float(100*np.sum(match)/max(1,np.sum(eligible))),
            'packet_boundary_energy_max_over_median':float(np.max(energy)/max(1,np.median(energy))),
            'strongest_boundary_phase':int(np.argmax(energy))})
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('wav',nargs='+');p.add_argument('--requests',type=int,default=32);a=p.parse_args()
    print(json.dumps([analyze(f,a.requests) for f in a.wav],indent=2))
