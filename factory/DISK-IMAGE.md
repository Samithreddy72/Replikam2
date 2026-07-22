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
| p3 | rootB | ext4 | `0d18cc81-03` | standby slot — populated **identically** at factory (A/B works from first boot) |
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

## CI wiring — the remaining step (NOT done yet)
To emit this from a tag push, a CI job must feed a rootfs + boot into the builder. Two
prerequisites on the rootfs build (`ci-build-image.sh`) first:
1. **Install `overlayroot` + build `initramfs612-overlay`** (`update-initramfs -c -k
   6.12.93+rpt-rpi-v8` with overlayroot present) so p1 carries the overlay-capable initramfs —
   otherwise read-only root will not engage (the builder warns when it's missing). Mirror the
   device's proven steps.
2. Decide **combined vs pure-bridge**: today's card is bridge+control-plane (ships fleet-brain);
   the CI rootfs is pure-bridge (fleet-brain stripped). A combined flashable image should source
   a rootfs that still carries fleet-brain. Part of the bridge-vs-combined split (see CI-BUILD.md).

Physical validation (flash a card + boot) is still pending — the structural build proves the
assembler, not a real boot.
