#!/usr/bin/env python3
"""Compare two initramfs images by what they CONTAIN, not by their bytes.

Every image build regenerates the initramfs, so the file's bytes always differ (timestamps in the
cpio headers). Two archives are equivalent when they hold the same paths with the same mode,
owner and content. Handles the Raspberry Pi layout: an uncompressed early cpio followed by a
compressed main cpio (zstd, gzip, xz or lz4).

  python3 tools/cpio-compare.py OLD NEW     exit 0 = same contents, 1 = different, 2 = unreadable
"""
import hashlib, subprocess, sys

MAGIC = {b"\x28\xb5\x2f\xfd": ["zstd", "-dc"], b"\x1f\x8b": ["gzip", "-dc"],
         b"\xfd7zXZ": ["xz", "-dc"], b"\x02\x21\x4c\x18": ["lz4", "-dc"]}


def parse(buf, out):
    off = 0
    while off < len(buf):
        if buf[off:off + 6] in (b"070701", b"070702"):
            h = buf[off:off + 110]
            f = [int(h[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
            mode, uid, gid, mtime, size, namesize = f[1], f[2], f[3], f[5], f[6], f[11]
            name = buf[off + 110:off + 110 + namesize - 1].decode("utf-8", "replace")
            data_off = (off + 110 + namesize + 3) & ~3
            data = buf[data_off:data_off + size]
            off = (data_off + size + 3) & ~3
            if name != "TRAILER!!!":
                out[name] = ((mode, uid, gid, size, hashlib.sha256(data).hexdigest()), mtime)
        elif buf[off:off + 1] == b"\0":
            off += 1
        else:
            for m, cmd in MAGIC.items():
                if buf[off:off + len(m)] == m:
                    parse(subprocess.run(cmd, input=buf[off:], capture_output=True, check=True).stdout, out)
                    return
            raise ValueError("unknown data at offset %d" % off)


def load(path):
    out = {}
    parse(open(path, "rb").read(), out)
    if not out:
        raise ValueError("no files in %s" % path)
    return out


def main(a, b):
    try:
        old, new = load(a), load(b)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        print("unreadable: %s" % e)
        return 2
    added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if old[k][0] != new[k][0])
    if added or removed or changed:
        print("%d added %s, %d removed %s, %d changed %s" % (len(added), added[:5], len(removed), removed[:5],
                                                            len(changed), changed[:5]))
        return 1
    stamps = sum(1 for k in old if old[k][1] != new[k][1])
    print("%d files, identical contents; %d differ only in timestamp" % (len(new), stamps))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
