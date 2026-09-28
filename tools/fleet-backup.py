#!/usr/bin/env python3
"""Consistent SQLite backup with integrity verification and optional age encryption.

Run on the fleet host during its normal backup job. Plain backups contain credentials;
keep them private. --recipient creates an encrypted file suitable for off-device storage.
This tool never uploads a file or changes the source database.
"""
import argparse, os, pathlib, sqlite3, tempfile, subprocess, shutil

def backup(source, destination, recipient=None):
    source=pathlib.Path(source).resolve(strict=True)
    destination=pathlib.Path(destination).resolve()
    if destination.exists(): raise ValueError('Refusing to overwrite an existing backup')
    if source == destination: raise ValueError('Backup must differ from source')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='nb-backup-',dir=destination.parent) as folder:
        raw=pathlib.Path(folder)/'fleet.db'
        with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(raw) as dst:
            src.backup(dst)
            result=dst.execute('PRAGMA integrity_check').fetchall()
            if result != [('ok',)]: raise RuntimeError('Backup integrity check failed')
        raw.chmod(0o600)
        candidate=raw
        if recipient:
            age=shutil.which('age')
            if not age: raise RuntimeError('Install age before requesting an encrypted backup')
            candidate=pathlib.Path(folder)/'fleet.db.age'
            subprocess.run([age,'-r',recipient,'-o',str(candidate),str(raw)],check=True)
            candidate.chmod(0o600)
        # Atomic no-clobber publication on the same filesystem.
        os.link(candidate,destination)
    return {'path':str(destination),'integrity':'ok','encrypted':bool(recipient),'uploaded':False}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',required=True);p.add_argument('--output',required=True)
    p.add_argument('--recipient',help='age public recipient for an encrypted backup')
    a=p.parse_args()
    import json
    print(json.dumps(backup(a.database,a.output,a.recipient),indent=2))
