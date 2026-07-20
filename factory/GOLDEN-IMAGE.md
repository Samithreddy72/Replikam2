# Creating the Golden Image (one-time, ~30 min)

The golden image is a fully-set-up RepliKam SD card, generalized, saved as a file.
Every fleet card is a byte copy of it + a per-unit identity conf.

## 1. Build the master card
- Flash + run `setup.sh` per docs/SETUP-GUIDE.md on ONE card
- Verify it works end-to-end (camera test in a meeting)

## 2. Generalize it (remove machine-specific identity)
SSH into the master Pi and run:
```
sudo systemctl stop bridge-agent.timer tailscaled
sudo rm -f /etc/bridge/agent.token            # forget enrollment
sudo tailscale logout 2>/dev/null; sudo rm -rf /var/lib/tailscale  # forget tailnet identity
sudo rm -f /etc/NetworkManager/system-connections/*   # forget wifi (firstboot/Imager re-adds)
sudo rm -f /etc/bridge/setup-wifi-pass        # forget the setup-AP key (each clone makes its own)
sudo systemctl enable bridge-firstboot 2>/dev/null    # arm firstboot for the clones
sudo poweroff
```
⚠️ Do not skip the `setup-wifi-pass` removal — if it stays in the image, every
bridge in the fleet ships with the SAME setup-AP password.

## 3. Image it (on the Mac)
Insert the master card, find it with `diskutil list external`, then:
```
sudo dd if=/dev/rdiskX of=~/Downloads/RepliKam2/factory/replikam-golden.img bs=8m status=progress
```
(For a smaller image, use a 16GB master card even if fleet cards are bigger.)

## 4. Stamp fleet cards
```
cp fleet.conf.example fleet.conf   # fill in the secrets
bash make-card.sh 2                # bridge-002
bash make-card.sh 3                # bridge-003 ... etc
```

## 5. Print each unit's label
Boot the stamped card once, then on that Pi:
```
bridge setup-pass
```
It prints the three things the label needs — setup Wi-Fi name, its **random
per-device password**, and the pairing code. The password is generated at first
boot and stored only at `/etc/bridge/setup-wifi-pass`; it cannot be recomputed
from the SSID, so if the label is lost the key must be read (or regenerated) here.

## Identity model (why clones don't collide)
- `device_id` = CPU serial → unique per Pi automatically
- `pairing_code` = derived from serial → unique automatically
- hostname + tailnet identity + enrollment → injected per card / created at first boot
- The bootstrap token is a shared fleet secret (the backend treats it as reusable)
