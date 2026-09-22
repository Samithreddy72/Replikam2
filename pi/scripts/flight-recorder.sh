#!/bin/bash
# Flight recorder: one line per second -> /home/pi/flight.txt (ring, last 500).
# The work is in flight-recorder.py (same line format, no program launches per second - the
# shell loop this replaced cost ~11% of a core). This name stays because the unit, the image
# audit and the diagnostics all refer to it.
exec /usr/bin/python3 /usr/local/bin/flight-recorder.py
