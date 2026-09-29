"""Run the final boot selector against real input configs and synthetic cpio archives."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('boot', ROOT / 'factory/configure-boot.py')
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)


def entry(name, data=b'', mode=0o100755):
    name = name.encode() + b'\0'
    fields = [1, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0]
    out = b'070701' + b''.join(('%08x' % v).encode() for v in fields) + name
    out += b'\0' * (-len(out) % 4)
    out += data
    return out + b'\0' * (-len(out) % 4)


class Boot(unittest.TestCase):
    def test_existing_usb_settings_never_skip_overlay_selection(self):
        for original in ('[all]\ndtoverlay=dwc2,dr_mode=peripheral\ninitramfs initramfs612 followkernel\n',
                         '[pi4]\nkernel=old.img\n[all]\nauto_initramfs=1\n', '[all]\n'):
            with self.subTest(config=original), tempfile.TemporaryDirectory() as directory:
                p = Path(directory)
                (p/'config.txt').write_text(original)
                (p/'kernel612.img').write_bytes(b'kernel fixture')
                (p/'initramfs612-overlay').write_bytes(entry('scripts/init-bottom/overlayroot', b'#!/bin/sh\n') + entry('TRAILER!!!'))
                boot.configure(p)
                result = (p/'config.txt').read_text()
                self.assertEqual(result.count('initramfs initramfs612-overlay followkernel'), 1)
                self.assertNotIn('initramfs initramfs612 followkernel', result)
                self.assertTrue(result.endswith('[all]\nauto_initramfs=0\nkernel=kernel612.img\ninitramfs initramfs612-overlay followkernel\n'))
                if 'dtoverlay' in original: self.assertIn('dtoverlay=dwc2,dr_mode=peripheral', result)

    def test_missing_hook_refuses_before_changing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p/'config.txt').write_text('[all]\n')
            (p/'kernel612.img').write_bytes(b'kernel fixture')
            (p/'initramfs612-overlay').write_bytes(entry('init', b'#!/bin/sh\n') + entry('TRAILER!!!'))
            with self.assertRaisesRegex(ValueError, 'overlayroot hook'): boot.configure(p)
            self.assertEqual((p/'config.txt').read_text(), '[all]\n')

if __name__ == '__main__': unittest.main()
