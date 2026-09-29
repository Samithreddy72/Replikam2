#!/usr/bin/env python3
"""Select and validate the final A/B overlay boot chain, independent of USB settings."""
import importlib.util
from pathlib import Path
import re
import sys


def configure(boot):
    boot = Path(boot)
    config = boot / 'config.txt'
    for name in ('config.txt', 'kernel612.img', 'initramfs612-overlay'):
        if not (boot / name).is_file() or not (boot / name).stat().st_size:
            raise ValueError('Missing boot file: ' + name)
    spec = importlib.util.spec_from_file_location('cpio', Path(__file__).resolve().parents[1] / 'tools/cpio-compare.py')
    cpio = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cpio)
    entries = cpio.load(boot / 'initramfs612-overlay')
    hook = entries.get('scripts/init-bottom/overlayroot')
    if not hook or not hook[0][0] & 0o111 or hook[0][3] == 0:
        raise ValueError('Selected initramfs lacks an executable overlayroot hook')
    # Remove conflicting selectors, including those inherited in conditional sections.
    lines = [line for line in config.read_text().splitlines()
             if not re.match(r'^\s*(?:kernel\s*=|auto_initramfs\s*=|initramfs\s+)', line)]
    config.write_text('\n'.join(lines).rstrip() + '\n\n[all]\nauto_initramfs=0\nkernel=kernel612.img\ninitramfs initramfs612-overlay followkernel\n')
    print('Verified overlay boot selection and executable initramfs hook')


if __name__ == '__main__':
    configure(sys.argv[1])
