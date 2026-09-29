#!/usr/bin/env python3
"""Enforce production service exclusions in each completed image slot."""
from pathlib import Path
import sys

MASKED = ('bridge-feeder.service', 'bridge-testpattern.service', 'ssh.service', 'ssh.socket',
          'bridge-idle-frame.timer', 'bridge-crackle-sentry.service', 'bridge-pitch.service')

def configure(root):
    root = Path(root).resolve()
    units = root / 'etc/systemd/system'
    units.mkdir(parents=True, exist_ok=True)
    if not units.resolve().is_relative_to(root):
        raise ValueError('systemd directory escapes image root')
    for name in MASKED:
        # Snapshot enablement and first-boot presets must not resurrect legacy
        # black/test producers or an SSH listener that bypasses bridge-ssh.
        for folder in list(units.glob('*.wants')) + list(units.glob('*.requires')):
            if folder.is_symlink():
                raise ValueError('refusing symlinked dependency directory')
            entry = folder / name
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
        target = units / name
        if target.is_symlink() or target.is_file():
            target.unlink()
        target.symlink_to('/dev/null')
    for name in MASKED:
        if (units / name).readlink() != Path('/dev/null'):
            raise ValueError('service mask not installed: ' + name)

if __name__ == '__main__':
    configure(sys.argv[1])
