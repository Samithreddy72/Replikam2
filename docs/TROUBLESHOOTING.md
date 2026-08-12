# Troubleshooting

**Read the Pi's own diagnostics first** (they solve 90% of mysteries):
```
ssh -i ~/.ssh/pi_bridge pi@<pi>   # then:
systemctl is-active bridge-gadget bridge-feeder-net bridge-uvcd bridge-feeder-audio bridge-return-audio
tail /home/pi/netlog.txt          # wifi/tailscale state, 30s samples
tail /home/pi/flight.txt          # 1s state snapshots (survives crashes)
journalctl -t wifi-guardian -n 20 # what the wifi self-healer did
journalctl -t jitter-sentry -n 10 # network-quality decisions
```

| Symptom | Cause → Fix |
|---|---|
| Camera shows black | No video source running → start `mac/go-live.sh` on the Mac. Camera app opened before streams were up → close and reopen it. |
| "UVC Camera" missing on client | Charge-only USB cable (very common!) → use a data cable. Or gadget down → reboot the Pi. |
| Pi reboots when client plugs in | Under-powered → see Golden Rule 5. |
| Pi frozen / vanished from network | If on kernel 6.18 → that's the freeze bug, run setup.sh's kernel step. Otherwise power-cycle; guardian + watchdogs recover everything at boot. |
| Meeting can't hear you | Meeting app muted? Mic = "Microphone (Source/Sink)"? Windows mic level 100? Watch the app's mic meter while talking. |
| You can't hear the meeting | Client: Speakers (Source/Sink) as default. Mac: volume up, `pgrep -f gst-launch` shows the listener; restart via stop-live + go-live. |
| Voice too quiet/loud | `MIC_GAIN_DB=14 bash mac/go-live.sh` (or 4 to reduce). Pi-side chain adapts automatically. |
| Return audio stutters | **Fleet → Diagnose it for me** names the cause. Usually lateness (Wi-Fi bursts or under-voltage) → **deeper buffer on your side**, which does not touch video. jitter-sentry also raises it automatically after ~1 min of trouble, even mid-session. |
| Meeting hears YOU breaking up | The other direction — that buffer is on the BRIDGE. **Fleet → The ROOM cannot hear YOU → deepen the bridge buffer.** ⚠ ~5 s video freeze. |
| Everything green, but no room audio | The meeting laptop is not playing INTO NetBridge. Select NetBridge as its **speaker/output** there. This is not the microphone setting, and no fleet action can fix it. |
| Audio suddenly worse than yesterday | **Fleet → Config column.** `drift N` means something changed since the state you verified. Restore known-good. |
| Pi unreachable at a new location | It raises its own hotspot "BridgeSetup-XXXX" after ~60s offline → connect a phone to it and pick the new Wi-Fi. |
| Corporate/office Wi-Fi blocks everything | Client isolation — use a phone hotspot for the Pi + Mac, or ask IT to whitelist the Pi's MAC. |

---

## Which end is broken?

The single most useful question, because the two directions have **opposite** fixes:

```
room  --mic--> BRIDGE --network--> PRESENTER   buffered on the PRESENTER'S laptop
presenter --network--> BRIDGE --usb--> room    buffered on the BRIDGE
```

*You* cannot hear *them* → your buffer → **jitter-fix** (no video interruption).
*They* cannot hear *you* → the bridge's buffer → **jitter profile** (⚠ freezes video).

Getting this backwards costs a five-second video freeze and changes nothing you can hear.

## Under-voltage looks exactly like a network fault

Stutter with **0 % packet loss**. The SoC throttles, so packets arrive **late, not lost**.
No network change fixes it and no software fix exists — a deeper buffer masks it. Check
`power.rate` in the fleet row or `power.txt` in a bundle; the live `throttled` field on older
cards reads `0x0` even mid-brownout.
