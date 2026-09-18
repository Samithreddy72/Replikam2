#!/usr/bin/env python3
"""Write a plain black 640x360 YUYV idle frame for the USB camera."""
import os
from pathlib import Path

W, H = 640, 360
OUT = "/etc/bridge/idle-frame.raw"


def render(output=OUT):
    # Limited-range BT.601 black, two pixels per YUYV macropixel.
    frame = bytes((16, 128, 16, 128)) * (W * H // 2)
    output = Path(output)
    if output.exists() and output.read_bytes() == frame:
        return False
    # The running UVC pump reloads on mtime changes. Never expose a partial frame.
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(frame)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output)
    return True


if __name__ == "__main__":
    print("Black idle frame " + ("updated" if render() else "unchanged"))
