# Flashable full-disk image (Journey 1: "flash as many cards as you need")

`factory/build-disk-image.sh` assembles a **whole-card** `.img` with the A/B + persistent
`/data` layout the fleet actually boots — the factory counterpart to the OTA path:

| | writes | when |
|---|---|---|
| `bridge-update.sh` (OTA) | ONE standby slot on a running card | field updates |
| `build-disk-image.sh` (factory) | a whole card from bare | provisioning a new unit |

## Layout produced (reproduced exactly from the proven dev card)
MBR signature `0x0d18cc81` ⇒ deterministic PARTUUIDs. The builder **fails loudly** if the
formatted PARTUUIDs don't come out as `0d18cc81-0{1..4}`.

| part | label | fs | PARTUUID | role |
|---|---|---|---|---|
| p1 | BRIDGEBOOT | vfat (bootable) | `0d18cc81-01` | firmware + `kernel612.img` + `initramfs612-overlay` |
| p2 | rootA | ext4 | `0d18cc81-02` | committed slot (`root=` in cmdline.txt) |
| p3 | rootB | ext4 | `0d18cc81-03` | standby slot — **EMPTY at factory** (fills on the first OTA; A/B rollback active after the first update). `POPULATE_ROOTB=1` ships it cloned instead. |
| p4 | bridgedata | ext4 | `0d18cc81-04` | **last** partition ⇒ grown to fill the card on first boot |

## Read-only overlay root is baked in here (and this is *why* it lives here, not in the CI image)
Each slot ships `overlayroot="tmpfs:recurse=0"` + `systemd-remount-fs` masked, and the
committed cmdline carries `overlayroot=tmpfs:recurse=0`. This is **safe on the disk image
but was NOT safe in `ci-build-image.sh`'s single-partition image**: overlay root sends all
writes to tmpfs, so it needs the separate `/data` (p4) partition — which only exists here — to
hold the DB / tailscale identity / `/etc/bridge` (via the `recurse=0` bind mounts). The builder
writes the authoritative real fstab per slot (overlayroot rewrites `/` into the overlay at boot).

## First-boot self-expand
The image ships a **minimal p4** so the `.img` stays small. `bridge-firstboot.sh` grows p4 to
fill the card on first boot (`growpart 4` / `parted resizepart 4 100%` → `resize2fs`), guarded
idempotent by `/data/.expanded`. Only ever touches partition 4 — never rootA/rootB. Marker on
`/data` ⇒ a reflash re-expands, an OTA (never rewrites `/data`) does not.

## Usage
```
sudo factory/build-disk-image.sh \
  --rootfs <rootfs.tar[.zst|.gz]>   # generalized root (from bridge-image-build / CI)
  --boot   <bootdir|boot.tar>       # p1 contents incl. kernel612.img + initramfs612-overlay
  --version X.Y.Z \
  --out    netbridge-os-X.Y.Z.img \
  --pubkey ota-pubkey.pem           # -> /data/config/ota-pubkey.pem (OTA root of trust)
# env: ROOT_MB (default 7168) DATA_MB (default 256) COMPRESS=1 (also emit .img.xz)
```
Runs on a Linux host / CI runner as root (loop devices + mkfs). **Verified structurally**
2026-07-23 on the Pi (small `ROOT_MB=48 DATA_MB=24` build): partition table, all four
PARTUUIDs/labels/fstypes, per-slot fstab (root 02 vs 03), overlayroot + remount-fs mask,
cmdline/config, ssh-host-key strip, `/data` skeleton + pubkey — all correct.

## CI wiring — DONE (`.github/workflows/build-image.yml` + `factory/ci-disk-from-image.sh`)
A tag push now produces + signs BOTH fleet artifacts from one arm-runner build:
- `rootfs.tar.zst` — the OTA payload `bridge-update.sh` consumes (manifest.txt, `image=rootfs.tar.zst`).
- `netbridge-os-<V>.img.xz` — this flashable full-disk image (manifest-disk.txt).
- plus `ota-pubkey.pem` (derived from the signing key) as the shipped root of trust.

`ci-build-image.sh` now also installs `overlayroot` and builds `initramfs612-overlay` so p1
carries the overlay-capable initramfs (else read-only root would not engage — the builder warns).
`ci-disk-from-image.sh` holds all the glue (loop-extract rootfs+boot → build-disk-image.sh →
manifests + EC signatures) so it is testable off-CI against any raspios-style `.img`.

**Verified off-CI** 2026-07-23 on the Pi: a synthetic 2-partition raspios image through
`ci-disk-from-image.sh` produced all artifacts, both manifests **signature-verified** against the
derived pubkey, OTA sha256 matched, and the disk image had correct PARTUUIDs / per-slot fstab /
overlay+remount-fs mask / host-key strip / `/data` skeleton.

### Known limits observed on the first real CI run (`v0.0.2-citest`, run 29959662109)
- The build+assemble+sign path **passed on a full-size image**: `rootfs.tar.zst` 1.11 GB,
  `netbridge-os-*.img.xz` 2.03 GB (8.8 GB raw), both manifests signed. Only the Publish step
  failed — a `gh` bug (missing `--repo` after `cd` out of the checkout), fixed.
- ✅ **RESOLVED — factory shrink.** rootB now ships **empty** (fills on first OTA), so the
  published `.img.xz` carries only ONE populated rootfs — roughly **halving** it (from ~2.0 GB
  to well under GitHub's 2 GiB cap) with comfortable headroom as the rootfs grows.
- ✅ **RESOLVED — faster `xz`.** With the smaller image, `ci-disk-from-image.sh` uses `xz -3`
  (was `-6`), tunable via `XZ_LEVEL`. The weaker ratio is now safe (plenty of margin under 2 GiB).
- Note: an OTA-filled rootB currently boots **writable** (the extracted `rootfs.tar.zst` carries
  no overlayroot config), unlike the read-only committed slot. Functional but inconsistent —
  making OTA'd slots read-only is a separate tracked item (would add the overlay config to
  `bridge-update.sh`'s standby write, in step with the device copy).

### Still pending
1. ✅ **A real CI run happened** (run 29959662109): build + assemble + sign all green; only
   Publish failed (now fixed). Re-tag to get a fully-green publish.
2. **Combined vs pure-bridge** — the CI rootfs is pure-bridge (fleet-brain stripped), so the disk
   image is a pure-bridge card. A combined flashable image needs a rootfs that still carries
   fleet-brain (bridge-vs-combined split, see CI-BUILD.md).
3. **Physical flash-boot test** — the structural build proves the assembler, not a real boot.
