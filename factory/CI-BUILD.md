# CI image build (Journey 1: "signed, secret-free image built by CI")

This replaces the manual `factory/GOLDEN-IMAGE.md` process (flash one card → run `setup.sh`
→ generalize by hand → `dd` a golden `.img`) with an automated GitHub Actions pipeline.

## What it produces
On a tag push (`vX.Y.Z`) or a manual run, `.github/workflows/build-image.yml`:
1. **Builds** a Raspberry Pi OS (arm64) image in an emulated Pi rootfs
   (`pguyot/arm-runner-action`), running `factory/ci-build-image.sh` — which replays
   `setup.sh` stages 2–7 (packages, pinned kernel 6.12.93, boot config, the `pi/` payload,
   Tailscale daemon **unjoined**, unit enablement) then **generalizes** the image.
2. **Signs** a `manifest.txt` (product, version, sha256, size, kernel, built) with your OTA
   key (`openssl dgst -sha256`) → `manifest.txt.sig`.
3. **Publishes** a GitHub release with `netbridge-os-<version>.img.xz` + manifest + signature.

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

## Known iteration points (first CI run will likely need a tweak or two)
- **DKMS on the pinned kernel** (`v4l2loopback` + `update-initramfs -c -k 6.12.93…`) is the
  fragile part in an emulated rootfs — needs the pinned headers + `gcc-12`, which we install;
  watch the arm-runner log here first.
- `raspios_lite_arm64:latest` boot-partition layout is assumed at `/boot/firmware`.
- The unit enable list mirrors `setup.sh` line 88 exactly; if a unit is renamed there, update
  both. The final "secret sweep" step in `ci-build-image.sh` fails loudly if a known secret
  pattern survives.
