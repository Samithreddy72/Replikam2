#!/usr/bin/env python3
"""Package a self-contained signed-loader input; publishing/signing is separate.

Older 2.2.0 images allow the feeder shell loader but not its Python dependency.
Embedding exact receiver source keeps the complete repair under one signature,
without extending the read-only update catalog or installing unsigned code.
"""
from pathlib import Path
import sys

HEADER = '#!/bin/bash\n# Signed self-contained receiver repair for OS 2.2.0-d950f2f.\n# The existing verifying loader authenticates this entire file, including Python.\nset -euo pipefail\nexport PATH=/usr/local/bin:/usr/bin:/bin\n[ ! -f /etc/default/bridge-net ] || . /etc/default/bridge-net\nVLAT=${NET_VIDEO_LATENCY:-100}\ncase "$VLAT" in \'\'|*[!0-9]*) VLAT=100 ;; esac\n[ "$VLAT" -le 100 ] || VLAT=100\n# Retire obsolete loopback writers; do not touch audio or the UVC pump.\nsystemctl mask --runtime --now bridge-feeder.service bridge-testpattern.service >/dev/null\nexec /usr/bin/python3 - "$VLAT" <<\'NETBRIDGE_RECEIVER_SOURCE\'\n'

def build(output):
    root = Path(__file__).resolve().parents[1]
    source = (root / 'pi/scripts/bridge-video-receiver.py').read_text()
    compile(source, 'bridge-video-receiver.py', 'exec')
    if '\nNETBRIDGE_RECEIVER_SOURCE\n' in source:
        raise ValueError('heredoc delimiter collision')
    Path(output).write_text(HEADER + source + '\nNETBRIDGE_RECEIVER_SOURCE\n')

if __name__ == '__main__':
    build(sys.argv[1])
