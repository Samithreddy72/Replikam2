# Remote recovery

Fixing a bridge you cannot physically reach.

The question this answers: *the Pi is in another country, something is wrong — what can I
actually do from here?*

---

## The short version

| Problem | Fix from the fleet page | Needs |
|---|---|---|
| Audio robotic / crackling | Reset audio clock | — |
| Return audio going nowhere | Re-issue mesh key | — |
| Nothing arriving at the bridge | Restart media services | interrupts |
| Presenter locked out | Clear lockout | — |
| Deployed code silently not running | Restore quarantined override | — |
| A bug needing a code change | **Push a code fix** | publish it first |
| A bad fix made it worse | Revert to factory | — |
| Bridge wedged | Reboot | interrupts |
| **Wi-Fi name changed at the venue** | ❌ **Nothing** — see below | someone on site |
| **Under-voltage / power** | ❌ Nothing — it is electrical | physical |

---

## Pushing a code fix to a bridge anywhere

This is the general escape hatch: any media script on the bridge can be replaced remotely.

### Why it works over any network

`deploy-script` tells the **bridge** to download a signed script from a URL — so that URL has
to be somewhere the bridge can reach. Serving it from a laptop does not work: a bridge on a
venue network cannot route back to you, and the fetch simply hangs.

The fleet is the answer. Every bridge already talks to `fleet.scine.online` over public HTTPS
every 15 seconds, from anywhere in the world. So the fleet hosts the payloads.

### How to do it

**1 · Sign and publish from your machine:**

```bash
cd ~/netbridge/replikam2-ci
bash tools/publish-script.sh pi/scripts/bridge-return-audio.sh
```

This signs the script with the private key in `~/.netbridge/keys/` (**which never leaves your
machine**), self-verifies, and uploads the script plus its detached signature to the fleet.

**2 · Deploy from the panel:**

**Actions → Push a code fix → deploy signed script…**, with the script name and
`https://fleet.scine.online/payloads` prefilled.

**3 · What the bridge does:**

```
fetch script + signature over HTTPS
verify the EC signature against a public key on the READ-ONLY root
syntax-check it
install to /data/overrides/
restart only that one service
```

The signature is verified **again at every service start**, so a payload tampered with later
still cannot run.

### Which scripts this works for

Anything systemd starts through the verifying loader: `bridge-return-audio.sh`,
`bridge-feeder-audio.sh`, `bridge-feeder-net.sh`, `bridge-uvcd.sh`.

Changing anything else — the agent, `bridge-web.py`, systemd units, `config.txt` — needs a
new OS image and a card flash.

---

## Auto-rollback, and getting back from it

If an override starts three times within 120 seconds, the loader treats it as a crash loop,
moves it to `/data/overrides/quarantine/`, and falls back to the factory script. **A bad
remote push can never brick a bridge.**

The catch: the bridge then runs factory code and **everything looks fine**. Nothing else on
the panel would tell you.

**So the fleet row shows `deployed code not running`**, and the fix is
**Actions → Deployed code not running → restore override**. It re-verifies the signature
before restoring (a file tampered with while quarantined stays quarantined) and clears the
start counter so the restored override gets a genuine fresh trial rather than resuming one
strike from tripping again.

> Historical note: the crash-loop counter used to be stamped with wall-clock time. Services
> start before NTP syncs, so every boot stamped nearly the same value — and **three power
> cycles looked identical to three crashes**, quarantining a perfectly healthy override. It
> is now counted against `/proc/uptime` and keyed by boot ID.

---

## What you genuinely cannot fix remotely

**A changed Wi-Fi network.** If the venue's SSID or password changes, the bridge cannot reach
the internet, so it cannot reach you. It falls back to raising its setup hotspot — which
needs somebody on site with a phone. This is unavoidable: the recovery channel and the broken
thing are the same channel.

**Power problems.** Under-voltage causes silent reboots that look like software faults.
Every software mitigation is already applied. Diagnose it from `power.txt` in a bundle
(sticky bits) — never from the panel's live `throttled` field, which reads `0x0` regardless.

---

## Before you ship a bridge somewhere

- [ ] Claim it and give it a recognisable name
- [ ] Set a PIN and record where you sent it
- [ ] Go live once and confirm all five checks green
- [ ] Note whether the path is **direct** or **relayed**
- [ ] Power-cycle it and confirm it returns on its own
- [ ] Collect one diagnostics bundle as a healthy baseline to compare against later
- [ ] Confirm whoever receives it can reach the setup portal from a phone

That last one is the only step that cannot be redone from a distance.
