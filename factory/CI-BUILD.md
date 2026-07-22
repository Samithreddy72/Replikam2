# CI image build (Journey 1: "signed, secret-free image built by CI")

This replaces the manual `factory/GOLDEN-IMAGE.md` process (flash one card → run `setup.sh`
→ generalize by hand → `dd` a golden `.img`) with an automated GitHub Actions pipeline.

## What it produces
On a tag push (`vX.Y.Z`) or a manual run, `.github/workflows/build-image.yml`:
1. **Builds** a Raspberry Pi OS (arm64) image in an emulated Pi rootfs
   (`pguyot/arm-runner-action`), running `factory/ci-build-image.sh` — which replays
   `setup.sh` stages 2–7 (packages, pinned kernel 6.12.93, boot config, the `pi/` payload,
   Tailscale daemon **unjoined**, unit enablement) then **generalizes** the image.
2. **Derives** the two fleet artifacts from that image (`factory/ci-disk-from-image.sh`):
   the OTA `rootfs.tar.zst` (for `bridge-update.sh`) and the flashable full-disk
   `netbridge-os-<version>.img.xz` (A/B + `/data` + read-only root — see `DISK-IMAGE.md`).
   `ci-build-image.sh` also builds `initramfs612-overlay` so the disk image can boot read-only.
3. **Signs** both manifests (`manifest.txt` for OTA, `manifest-disk.txt` for the disk image)
   with your OTA key (`openssl dgst -sha256`), and derives+ships `ota-pubkey.pem`.
4. **Publishes** a GitHub release with the disk image, the OTA rootfs, both signed manifests,
   and the OTA public key.

## Secret-free = generalized
`ci-build-image.sh` strips every per-device identity, so one image flashes any number of
cards (the label + first boot make each unit unique):
- no Tailscale identity (`/var/lib/tailscale` emptied — key-at-claim via `/v1/provision`),
- no Wi-Fi PSKs (`/etc/NetworkManager/system-connections` — onboard via the setup portal),
- no agent token / bootstrap conf / setup-AP passphrase (generated per-device by
  `bridge-firstboot.service` on first boot),
- no SSH host keys (regenerated per-device by `bridge-regen-hostkeys.service`).
- fleet-brain (the control plane) is **not** in the bridge image at all.

The pubkey that verifies these images already lives on devices at
`/data/config/ota-pubkey.pem` (and should ship in the image root of trust too).

## The ONE thing you must do
Add the signing **private key** as a repo secret named **`OTA_SIGNING_KEY`**
(Settings → Secrets and variables → Actions → New secret), pasting the PEM from
`~/Downloads/netbridge-ota-keys/ota-signing-key.pem`. Until then the workflow builds and
publishes but marks the release **UNSIGNED** (with a warning) rather than failing.

```
gh secret set OTA_SIGNING_KEY < ~/Downloads/netbridge-ota-keys/ota-signing-key.pem
```

## New files this adds
- `.github/workflows/build-image.yml` — the pipeline
- `factory/ci-build-image.sh` — the chroot provisioning + generalize
- `pi/systemd/bridge-firstboot.service` — the first-boot unit `bridge-firstboot.sh` expected but that was missing
- `pi/systemd/bridge-regen-hostkeys.service` — per-device SSH host-key regen

## A/B signed-OTA tooling now ships in the image
Earlier the image baked the obsolete `pi/scripts/bridge-update.sh` (a naive `git pull` updater)
and had **no `bridge-ab` at all** — so a flashed card could not run the signed A/B OTA the
fleet actually uses. Synced from the proven on-device copies:
- `pi/scripts/bridge-update.sh` — signed-manifest A/B OTA (verify EC sig → write STANDBY slot
  → arm auto-commit → `bridge-ab tryboot`); replaces the git-pull version.
- `pi/scripts/bridge-ab` — A/B slot mgmt via Pi tryboot (`status|tryboot|commit|rollback|healthcheck`).
- `pi/scripts/bridge-image-build` — snapshot running root → signed-manifest OTA package.
- `pi/systemd/bridge-ab-healthcheck.service` — enabled; **no-op on a normal boot**, only gates a
  `bridge_tryboot=1` trial (healthy → auto-commit, unhealthy → auto-rollback).

⚠️ Known caveat (part of the bridge-vs-combined split, tracked separately): `bridge-ab`'s
health gate requires `fleet-brain` + `:8000/healthz`. On a **pure-bridge** image (fleet-brain
stripped) a trial would fail that gate and roll back. Fine today — the A/B cards are the
combined dev-Pi lineage; the pure-bridge health criteria are a follow-up when the control
plane is split off.

## Drift sync: image now matches the proven device state
The image provisioning had drifted behind the running bridge. Reconciled:
- **Hardware watchdog armed** — `pi/configs/kit-watchdog.conf` is now `RuntimeWatchdogSec=15`
  (was `0`). The old `0` dated to commit f370860's boot-with-client reboot-spiral concern;
  that case was re-tested 2026-07-23 (client enumerated, armed, rebooted) and did **not**
  spiral. RebootWatchdogSec=2min bounds a hung shutdown.
- **`bridge-diagnose.sh`** — adds mesh / gadget-AV / media-health capture and uses
  `vcgencmd get_throttled` for the power section (sticky bits, not dmesg).
- **`bridge-feeder.sh` + `bridge-up-all.sh`** — idle test-pattern bumped to 640x360@20
  (local-test / manual bring-up path; production is `bridge-feeder-net`).
- **`bridge-powertrim.sh`** — re-enables the ACT (mmc0) + PWR LEDs for unit-status
  visibility instead of blanking them (a few mA; aids field diagnosis).

## Deferred: read-only (overlay) root — NOT shipped here, and why
Read-only overlay root (`overlayroot="tmpfs:recurse=0"`) is live on the dev card but is
**deliberately not baked into this CI image yet.** It depends on the separate `/data`
partition to hold persistent writes (DB, tailscale identity, `/etc/bridge`). This CI image is
still a single-partition raspios layout (boot + root, **no `/data`, no A/B**) — enabling
overlayroot on it would send *every* write to tmpfs and lose it on reboot (the exact
`recurse=0` bug, but unavoidable with no `/data` to exclude). Read-only root must therefore
land **together with the flashable full-disk image** (the `--disk` build that creates
BRIDGEBOOT + rootA + rootB + `/data`). Tracked there, not here.

## Known iteration points (first CI run will likely need a tweak or two)
- **DKMS on the pinned kernel** (`v4l2loopback` + `update-initramfs -c -k 6.12.93…`) is the
  fragile part in an emulated rootfs — needs the pinned headers + `gcc-12`, which we install;
  watch the arm-runner log here first.
- `raspios_lite_arm64:latest` boot-partition layout is assumed at `/boot/firmware`.
- The unit enable list mirrors `setup.sh` line 88 exactly; if a unit is renamed there, update
  both. The final "secret sweep" step in `ci-build-image.sh` fails loudly if a known secret
  pattern survives.
