"""Consistent bounded local Fleet backups. Off-host backup needs operator storage configuration."""
import argparse
import datetime as dt
import os
from pathlib import Path
import sqlite3
import tempfile
import time


def backup(database, directory, retain=14, now=None):
    database, directory = Path(database), Path(directory)
    if not database.is_file():
        raise ValueError('Fleet database does not exist')
    if not 1 <= retain <= 90:
        raise ValueError('Retention must be between 1 and 90 daily backups')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    date = (now or dt.datetime.now(dt.timezone.utc)).strftime('%Y-%m-%d')
    target = directory / ('fleet-daily-' + date + '.sqlite3')
    fd, name = tempfile.mkstemp(prefix='.fleet-backup-', dir=directory)
    os.close(fd)
    tmp = Path(name)
    try:
        source = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
        destination = sqlite3.connect(tmp)
        try:
            deadline = time.monotonic() + 120
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise TimeoutError('Database stayed busy beyond the backup deadline')
            source.backup(destination, pages=256, sleep=.05, progress=progress)
            if destination.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise RuntimeError('Backup integrity check failed')
        finally:
            destination.close()
            source.close()
        with tmp.open('rb') as f:
            os.fsync(f.fileno())
        os.replace(tmp, target)
        parent = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        # Never delete pre-deploy copies or unrelated operator files.
        backups = sorted(directory.glob('fleet-daily-????-??-??.sqlite3'), reverse=True)
        for obsolete in backups[retain:]:
            if obsolete.is_file() and not obsolete.is_symlink():
                obsolete.unlink()
        return target
    finally:
        tmp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', default='/data/bridge.db')
    parser.add_argument('--directory', default='/data/backups')
    parser.add_argument('--retain', type=int, default=14)
    args = parser.parse_args()
    print('Verified Fleet backup:', backup(args.database, args.directory, args.retain).name)


if __name__ == '__main__':
    main()
