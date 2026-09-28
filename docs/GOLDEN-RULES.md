# Golden Rules (each one was learned the hard way)

1. **NEVER upgrade the kernel past 6.12.x.** Kernel 6.18's dwc2 USB driver hard-freezes
   the whole Pi the instant a laptop connects. setup.sh pins 6.12.93 and apt-holds the
   kernel packages — leave both alone.
2. **Never restart `bridge-uvcd` (or the gadget) while the client laptop is plugged in.**
   Unplug → restart → replug. Hot-restarting under an attached host can hang the USB driver.
3. **Plug the client once, before the meeting.** Hot-replugging mid-call can reboot a
   marginally-powered Pi and always resets Windows audio defaults.
4. **After ANY client replug, re-check Windows:** mic/speaker = Source/Sink, levels = 100,
   meeting app device selection (they silently reset).
5. **Power is physics.** If the Pi resets at laptop connection or go-live, inspect the supply,
   voltage drop and power/data isolation during maintenance. Do not improvise parallel USB/GPIO
   supplies or assume a capacitor fixes sustained undervoltage. Follow [power acceptance](POWER-ACCEPTANCE.md).
6. **You will never hear your own voice in Meet/Zoom/Teams** — they don't loop your mic
   back. Verify with the app's mic level meter or another participant.
7. **Echo = Mac speakers leaking into the Mac mic.** Wear headphones on the Mac
   (go-live auto-routes to "Bassheads 100 C"; edit HEADPHONES= for another headset).
8. **v4l2loopback must never be `rmmod`ed on a live system** — it hangs the kernel. Reboot instead.
9. **The test pattern is your friend:** `sudo systemctl stop bridge-feeder-net && sudo systemctl start bridge-testpattern`
   puts SMPTE bars on the camera — if the client sees bars, everything but your Mac's sender works.
   (Reverse to go back to the real camera.)
