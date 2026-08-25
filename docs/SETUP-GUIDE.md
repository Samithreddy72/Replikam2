# Setup Guide — blank SD card to a working bridge

Written for someone who has never touched a Raspberry Pi. Every command can be
copy-pasted. Where you must substitute a value, it looks `LIKE_THIS`.

> **Which guide is this?** This is the **normal** path: flash a prebuilt image and the
> bridge sets itself up. If you are developing NetBridge itself and want to build a card
> from source, see [Appendix B](#appendix-b--building-a-card-from-source) at the end.

---

## What NetBridge does, in one paragraph

A Raspberry Pi pretends to be an ordinary USB webcam, microphone and speaker. You plug it
into a meeting laptop anywhere in the world and it appears in Zoom/Teams/Meet with no
software installed on that laptop. A presenter elsewhere runs the NetBridge app, and their
camera and voice come out of that fake webcam — while the meeting room's audio comes back
to them. The two ends find each other over a private encrypted mesh; neither needs a public
IP, a port forward, or to be on the same network.

---

## Phase 0 — What you need

### Hardware

| Item | Notes |
|---|---|
| Raspberry Pi 4 Model B | 2 GB RAM or more. **Pi 4 specifically** — the USB gadget mode this depends on is not available the same way on other models. |
| microSD card, 16 GB or larger | High-endurance cards last longer. Plus an adapter to plug it into your computer. |
| Power supply for the Pi | The Pi 4 draws up to ~1.2 A under load. An underpowered supply or a thin cable causes random reboots that look like software faults. See [Troubleshooting](#phase-10--troubleshooting). |
| **USB-C data cable** | Pi's USB-C port → meeting laptop. ⚠️ Many USB-C cables are charge-only and have no data wires. If the laptop never sees a webcam, suspect the cable first. |
| A laptop for the presenter | macOS (Apple Silicon) or Windows 10/11. Both are released. |
| Wi-Fi at the bridge's location | Any normal network. No port forwarding, no static IP, no router changes. |

### Accounts and services

| Item | Why | Who provides it |
|---|---|---|
| A NetBridge fleet URL | Where bridges report in and where you manage them | Already deployed for this project: `https://fleet.scine.online` |
| A sign-in email | You sign in with a magic link — there is no password | Yours |

### On the presenter's machine

**Nothing to install.** The app ships with its own ffmpeg and GStreamer, so the presenter
downloads one file and runs it.

Get it from the repository's **Releases** page — the `app-v*` release, not the OS image:

| You are on | Download |
|---|---|
| macOS (Apple Silicon) | `NetBridgeSource-macos-arm64.zip` |
| Windows 10/11 | `NetBridgeSource-windows-x64.zip` |

Unzip it and **keep every file in the folder together.** The `netbridge-mesh` helper sitting
beside the app is what carries the meeting's audio back to the presenter — separate them and
the meeting will still see and hear you while you hear nothing, with no obvious error.

The builds are not code-signed, so each OS asks once:

```bash
# macOS — clear the download quarantine
xattr -dr com.apple.quarantine .   # the WHOLE folder — the mesh helper is quarantined too
```

On Windows, SmartScreen shows "Windows protected your PC" → **More info** → **Run anyway**.

First launch takes about 15 seconds while the bundle unpacks. It is not hung.

---

## Phase 1 — Prepare the SD card

### 1.1 Get the image

Download the latest release from the repository's **Releases** page. You want the file named:

```
netbridge-os-VERSION.img.xz
```

> ⚠️ The same release also contains `rootfs.tar.zst`. **That is not flashable** — it is the
> payload used for over-the-air updates of an already-running bridge. Flashing it will not
> work.

### 1.2 Verify what you downloaded (recommended)

The release includes `manifest-disk.txt` and a signature. Check the image is intact:

```bash
shasum -a 256 netbridge-os-VERSION.img.xz
cat manifest-disk.txt
```

The `sha256=` line in the manifest must match the number `shasum` printed. If it does not,
the download is corrupt — re-download rather than flashing it.

### 1.3 Flash it

1. Install **Raspberry Pi Imager** from `raspberrypi.com/software`.
2. **Choose Device:** Raspberry Pi 4
3. **Choose OS:** scroll to the bottom → **Use custom** → select the `.img.xz` you downloaded
4. **Choose Storage:** your SD card — *check this twice, flashing erases the card*
5. Click **NEXT**. When it asks about customisation settings, choose **NO / Edit Settings →
   nothing**.

> **Why no settings?** With stock Raspberry Pi OS you would type a hostname, a password and
> Wi-Fi details here. This image does not need any of that — it has its own setup flow, and
> every card is identical. That is deliberate: there is nothing per-device to get wrong.

Wait for flashing and verification to finish, then eject the card.

---

## Phase 2 — First boot and Wi-Fi

1. Put the SD card in the Pi.
2. Connect power. **Wait about 90 seconds** — the first boot is slower than later ones.
3. On your phone, open Wi-Fi settings. A new network appears:

   ```
   BridgeSetup-XXXX
   ```

   The password is on the label that shipped with the bridge.

4. Join it. A setup page opens automatically (this is a *captive portal*, the same mechanism
   hotel Wi-Fi uses).
5. Pick the venue's Wi-Fi network from the list, type its password, press **Connect**.
6. The page shows three ticks as they happen:

   ```
   ✓ Wi-Fi connected
   ✓ Internet reached
   ✓ Online with your fleet — you can close this page
   ```

That third tick is the important one: the bridge has found the fleet by itself.

> **The setup network disappears once the bridge is online.** That is intentional — it only
> exists when the bridge has no internet. If you ever need it again (new venue, changed
> Wi-Fi password), power the bridge somewhere its known networks are absent and it comes back.

---

## Phase 3 — Claim the bridge

1. Open `https://fleet.scine.online` and sign in with your email (magic link, no password).
2. A new row appears at the top of the fleet list:

   ```
   🆕 Unclaimed · BRIDGE-XXXX      just joined
   ```

   `BRIDGE-XXXX` matches the code printed on the bridge's label.
3. *(Optional but useful)* Before claiming, use the **Check…** dropdown on that row —
   `Collect diagnostics`, `Fetch recent logs`, `What code is it running?` — to confirm it is
   the box you think it is and that it is healthy.
4. Click **Claim**, give it a name a human would recognise (`Mumbai Studio`, `NYC Boardroom`).

Claiming binds the bridge to your organisation and issues its mesh key. **That is the entire
enrolment ceremony** — there is no key to copy onto the card, and nothing was configured
per-device.

---

## Phase 4 — Set a PIN (recommended)

Seeing a bridge in the list does not let someone stream to it — the device itself checks a
PIN before accepting a single frame.

**Actions → Set / rotate PIN…**, choose 6 digits, and give them to the presenter *offline*
(a call, a text). The panel never displays a PIN again after you set it.

---

## Phase 5 — Plug in the meeting laptop

Connect the Pi's **USB-C port** to the meeting laptop with a **data** cable.

The laptop sees a normal webcam and microphone called **NetBridge**. Nothing installs.

Until a presenter goes live, the camera shows a status card:

```
        NetBridge
   Bridge XXXX is online
Waiting for your presenter to go live…
    ✓ Wi-Fi  ✓ Internet  ✓ USB host
```

Select **NetBridge** as the camera *and* microphone in Zoom/Teams/Meet. For the room to hear
the presenter, also select NetBridge as the **speaker**.

---

## Phase 6 — The presenter's computer

Install the NetBridge app on the presenter's Mac and launch it. Then:

1. **Sign in** with your work email — a magic link arrives, click it.
2. **Pick** the bridge, camera and microphone. Your last choices are remembered.
3. **Enter the PIN** the admin gave you and click **Unlock bridge** *(only if the bridge is
   locked — if it is not, skip straight to Go live)*.
4. Click **Go live.**

---

## Phase 7 — Verify it works

The app shows five checks. All five green means the whole chain is working:

| Check | What it proves |
|---|---|
| Bridge online | The app can reach the bridge over the mesh |
| Your video arriving at bridge | Your camera is encoded and landing on the Pi |
| Your voice arriving at bridge | Same for your microphone |
| Meeting laptop sees the camera | The USB gadget is enumerated by the laptop |
| Meeting audio flowing back | The room's audio is reaching you |

The status line underneath tells you the path:

```
direct mesh path                              ← best possible latency
⚠ relayed path (derp) — higher latency        ← works, but slower
```

On a long link (say India → North America) that difference matters: a direct path is roughly
200–250 ms, a relayed one is often double that.

> **"Meeting audio flowing back" is red but everything else is green.** Usually correct: it
> only turns green when the meeting laptop is actually *playing* something. Play any sound on
> that laptop and it goes green.

**Installation is successful when:**

- [ ] Bridge boots and the setup portal appears
- [ ] Portal shows all three ticks
- [ ] Bridge appears as Unclaimed in the fleet within a minute
- [ ] Claim succeeds and the row shows **online**
- [ ] Meeting laptop lists **NetBridge** as camera, mic and speaker
- [ ] Presenter can sign in and go live
- [ ] All five checks are green
- [ ] Room hears the presenter; presenter hears the room
- [ ] After a power cycle, the bridge comes back online **on its own**

That last one matters most — it is the difference between a demo and a deployment.

---

## Phase 8 — It already starts automatically

Nothing to configure. Every service is a systemd unit enabled in the image, the root
filesystem is read-only (so a power cut cannot corrupt it), a hardware watchdog reboots the
Pi if it ever locks up, and the bridge re-enrols itself with the fleet on every boot.

Pull the power at any moment and it comes back.

---

## Phase 9 — Day-to-day operation

Everything is done from the fleet page. **Actions** is grouped by risk — read-only at the
top, anything that interrupts a live stream at the bottom:

| Group | Use it for |
|---|---|
| **Look first** | Diagnostics, recent logs, what code the bridge is running. Changes nothing. |
| **Repair** | Robotic or crackling audio, return audio going nowhere, restoring code the bridge quietly rolled back |
| **Access** | Set or rotate the PIN, clear a lockout after three wrong tries |
| **Interrupts the stream** | Restart media, change jitter profile, reboot |

Anything in the last group asks for confirmation, and warns you harder if someone is
streaming right now.

---

## Phase 10 — Troubleshooting

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| No `BridgeSetup-XXXX` network | Pi not powered, or already online | Is the power LED on? Is it already in the fleet? | Wait 90 s after power-on. The portal only appears when the bridge has *no* internet. |
| Portal opens but Wi-Fi won't connect | Wrong password, or a 5 GHz-only network the Pi cannot see | Re-check the password | Try the 2.4 GHz band if the venue has one |
| Bridge never appears in the fleet | It has Wi-Fi but no internet | Portal shows "Wi-Fi connected" but not "Internet reached" | Captive-portal networks (hotels, cafés) need a browser login first — the bridge cannot do that. Use a network without one, or a phone hotspot. |
| Laptop sees no webcam | Charge-only USB-C cable | Try another cable | Use a known **data** cable. This is the single most common cause. |
| Random reboots mid-meeting | Insufficient power | Fleet → Collect diagnostics → look at `power.txt` for `throttled=0x50000` | Under-voltage. Shorter/thicker cable and a supply rated for the Pi 4's peak draw. ⚠️ **The live `throttled` field on the panel reads `0x0` even during a brownout — only the diagnostics bundle tells the truth.** |
| Audio sounds robotic or pitch-shifted | Capture rate ≠ pipeline rate | Usually self-heals in ~10 s | **Actions → Audio robotic or crackling → reset audio clock** |
| Presenter hears nothing from the room | Meeting laptop isn't outputting to NetBridge | Laptop's sound settings | Select **NetBridge** as the *speaker*, not just the microphone |
| Presenter can't unlock | Three wrong PINs locks the bridge for an hour | Fleet row shows the lockout | **Actions → Presenter locked out → clear lockout…** |
| Video green in app, nothing at the bridge | The app is encoding into nothing | App's own status line | End session → Go live. The app now detects this itself and reconnects. |
| Everything green but the call is laggy | Relayed instead of direct mesh path | Status line says `⚠ relayed path` | The venue's NAT is blocking a direct connection. A different network usually fixes it; otherwise the relay still works, just slower. |

**When you cannot tell what is wrong:** Fleet → **Actions → Collect diagnostics**. It gathers
service state, logs, USB state, power history and an audio analysis into one downloadable
bundle.

---

## Appendix A — What was verified, and what was not

Honesty matters more than a clean-looking guide.

**Verified on real hardware:** the captive portal, self-enrolment into the fleet, claiming,
PIN gating, all five checks going green, live sample-rate switching between 48/44.1/32 kHz,
survival of repeated power cuts, and remote actions from the fleet page.

**Not verified:** flashing *this specific release* onto a *blank* card and booting it. Earlier
image builds in this project's history produced un-bootable cards because of a boot-partition
layout bug. **Test-boot a new image on a spare card before you rely on it**, and confirm it
reaches the fleet before shipping the bridge anywhere.

**Known limitation:** echo cancellation is built but not enabled, so a presenter who is also
in the meeting audibly may hear themselves. Use headphones.

---

## Appendix B — Building a card from source

Only needed if you are changing NetBridge itself.

```bash
git clone https://github.com/Samithreddy72/Replikam2.git
cd Replikam2
gh workflow run build-image.yml
```

The build takes roughly 25 minutes and publishes a release with the same assets described in
Phase 1. The fleet URL and enrolment token are baked in from repository variables
(`FLEET_CONTROL_URL`, `FLEET_BOOTSTRAP_TOKEN`), which is what makes a flashed card find its
fleet with no configuration.

> ⚠️ The enrolment token is baked into the image. Anyone holding the image file can enrol a
> device into your fleet. That is inherent to "no per-device configuration" — keep image
> files as private as the repository, and rotate `BOOTSTRAP_TOKENS` if one ever leaks.

See [PROVISIONING-V2.md](PROVISIONING-V2.md) for the provisioning internals and
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) for deeper diagnostics.
