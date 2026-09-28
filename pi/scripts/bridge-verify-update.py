#!/usr/bin/env python3
"""Signed context and monotonic acceptance floor for NetBridge script updates."""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

PREFIX = b'# NetBridge-Update: '
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}')

def valid_name(name):
    return bool(NAME.fullmatch(name)) and '..' not in name

def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.'+path.name+'-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def prepare(source, destination, name, revision):
    if not valid_name(name) or not isinstance(revision, int) or not 0 < revision < 2**63:
        raise ValueError('invalid update identity or revision')
    body = pathlib.Path(source).read_bytes()
    lines = body.splitlines(keepends=True)
    index = 1 if lines and lines[0].startswith(b'#!') else 0
    if len(lines)>index and lines[index].startswith(PREFIX): lines.pop(index)
    if any(line.startswith(PREFIX) for line in lines): raise ValueError('reserved metadata marker in source')
    context = {'product':'netbridge-script', 'name':name, 'revision':revision}
    marker = PREFIX + json.dumps(context, separators=(',',':'), sort_keys=True).encode() + b'\n'
    index = 1 if lines and lines[0].startswith(b'#!') else 0
    lines.insert(index, marker)
    pathlib.Path(destination).write_bytes(b''.join(lines))

def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate metadata key')
        result[key] = value
    return result

def verify(payload, signature, public_key, name, floor_dir, accept=False):
    if not valid_name(name): raise ValueError('invalid update name')
    p = pathlib.Path(payload)
    body = p.read_bytes()
    if not 0 < len(body) <= 2*1024*1024: raise ValueError('invalid payload size')
    result = subprocess.run(['openssl','dgst','-sha256','-verify',str(public_key),'-signature',str(signature),str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode: raise ValueError('signature verification failed')
    markers = [line[len(PREFIX):] for line in body.splitlines() if line.startswith(PREFIX)]
    if len(markers) != 1: raise ValueError('exactly one signed update context required')
    meta = json.loads(markers[0], object_pairs_hook=unique_json)
    if not isinstance(meta,dict) or meta.get('product') != 'netbridge-script' or meta.get('name') != name:
        raise ValueError('signed update identity mismatch')
    revision = meta.get('revision')
    if type(revision) is not int or not 0 < revision < 2**63: raise ValueError('invalid signed revision')
    digest = hashlib.sha256(body).hexdigest()
    floor = pathlib.Path(floor_dir)/name
    try:
        old = json.loads(floor.read_text())
    except FileNotFoundError:
        old = {'revision':0}
    if not isinstance(old,dict) or type(old.get('revision')) is not int:
        raise ValueError('security floor unreadable')
    if revision < old['revision'] or (revision == old['revision'] and digest != old.get('sha256')):
        raise ValueError('update replay or conflicting revision refused')
    if accept:
        atomic_write(floor, json.dumps({'revision':revision,'sha256':digest}).encode()+b'\n')
    return meta

def main():
    try:
        if sys.argv[1] == 'prepare':
            prepare(*sys.argv[2:5], int(os.environ.get('NB_UPDATE_REVISION') or time.time_ns()))
        elif sys.argv[1] == 'verify':
            verify(*sys.argv[2:7], accept=len(sys.argv)>7 and sys.argv[7]=='accept')
        else: raise ValueError('unknown operation')
    except (OSError, ValueError, IndexError, TypeError) as exc:
        print('update context refused: '+str(exc), file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__': sys.exit(main())
