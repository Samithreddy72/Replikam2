# NetBridge power diagnosis and acceptance

Prepared 2026-09-29. This is a maintenance-window test plan, not a claim that hardware has been repaired. Do not reconnect USB, restart the gadget, flash a card, or change wiring during a meeting.

## Evidence specific to this installation

The 28 September audit recorded NB-002 at approximately 41–42 °C, a `0x50005` throttle mask, and eventually active undervoltage in 100% of the last 500 flight-recorder samples. Video pumping remained close to 30 fps while USB transfers reported misses. These are firmware flags and counters, not independent rail-voltage measurements or proof of what the meeting app rendered.

Claude's historical notes describe a reset four seconds after go-live and an association between brownout sampling rates and audio quality across eight labelled sessions. Treat this as supporting evidence, not proof that every audio problem is electrical. The current physical wiring and supply have not been inspected in this pass.

## Priorities

1. **High reward: identify voltage drop in the existing power path.** In an idle maintenance window, document where power enters the Pi, the source's actual 5 V capability, cable length/gauge, connectors, and whether laptop USB VBUS is connected alongside another supply. Have a competent technician measure voltage at the board under idle, go-live and sustained full media load. Short transients may need an oscilloscope; a normal multimeter reading does not rule them out. Repair loose/high-resistance connections and test a short, known-good data/power cable. Do not join two supplies with a passive Y cable or improvise GPIO/USB backfeeding.
2. **High reward: qualify simultaneous gadget data and power.** Pi 4's USB-C port is also the gadget port, so simply replacing its laptop connection with a charger loses the camera/audio data path. A properly designed power/data arrangement must preserve that path and prevent upstream backfeed. Raspberry Pi recommends externally powered USB hubs for weak host ports, but the particular hub's per-port current capability must be established: 3 A total input does not establish 3 A at one port. No accessory is certified for this NetBridge setup until tested under load.
3. **Medium reward: reduce avoidable load transitions.** Keep idempotent peer configuration, suppress restart storms, and test presenter identity continuity so normal reconnects do not unnecessarily restart room audio. These reduce triggers; they cannot guarantee stable voltage. Persistent identity requires revocation and cleanup as well as saved client state, and is not yet implemented in this branch.
4. **Medium reward: make power evidence actionable.** Show active undervoltage percentage over a labelled sample window, current throttling, historical flags, boot ID/uptime and USB misses separately. Correlate each with go-live and restart timestamps. Never label a stale sample healthy.
5. **Low reward: peripheral trimming.** Keep the tested 900 MHz ceiling during the baseline. The old script incorrectly logged 1.2 GHz. Lowering it further can reduce decoding headroom. Ethernet or USB-host shutdown must eventually become deliberate configuration, not a hidden surprise for administrators. Current candidate only corrects the frequency log; it does not change these power policies.

A larger audio jitter buffer can mask late packets but adds delay and cannot prevent a brownout reset. Do not change the verified audio settings as a power repair. A capacitor can only bridge short transients and adds inrush concerns; the old blanket 2200 µF GPIO recommendation is not a verified fix for persistent undervoltage. A UPS is useful for input outages only if its regulated output also survives the load.

## Acceptance procedure

Record the existing supply/cable arrangement and software identities. Keep codec, resolution, frame rate and audio settings fixed. Change one physical variable at a time, then collect the same duration of evidence.

- After a clean boot, record `vcgencmd get_throttled`, actual CPU frequency/ceiling, temperature, boot ID and service restart counts.
- Run at least 30 minutes of representative full media load and several planned idle-to-live transitions. Inspect a real receiver on both Windows and macOS for frozen video, USB reconnects and audible gaps.
- Target: no active undervoltage samples, no new undervoltage/throttle history after the clean boot, no unexpected resets/restarts, stable camera cadence, and no reproducible receiver failures. Sampling cannot exclude every transient; investigate physical measurements if resets persist.
- `0x50000` means historical undervoltage and throttling. `0x50005` additionally means undervoltage and throttling now. Clearing history by rebooting is not a repair.
- Compare USB misses and observed latency before/after. Do not claim all USB misses were power-caused merely because power improved.

## Official references

- Raspberry Pi [power supply requirements](https://www.raspberrypi.com/documentation/computers/getting-started.html): Pi 4 requires a suitable 5 V / 3 A supply; cable drop matters at the plug.
- Raspberry Pi [15 W supply specification](https://www.raspberrypi.com/products/type-c-power-supply/): 5.1 V / 3 A, useful as an electrical benchmark, not a requirement to buy a particular accessory.
- Raspberry Pi [voltage monitoring](https://www.raspberrypi.com/documentation/computers/config_txt.html): undervoltage detection around 4.63 V (tolerance applies), with throttling; PMIC ADC instructions for Pi 5 do not apply to this Pi 4.
- Raspberry Pi [gadget-mode hardware guidance](https://www.raspberrypi.com/news/usb-gadget-mode-in-raspberry-pi-os-ssh-over-usb/): weak host ports can cause link drops and reboots; the power-path guidance applies, but do not install its USB-network gadget package over NetBridge's UVC/UAC2 gadget.
- Raspberry Pi [throttle bit definitions](https://www.raspberrypi.com/documentation/computers/os.html#get_throttled).
