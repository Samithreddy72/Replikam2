#!/usr/bin/env python3
"""Separate SOURCE GAPS from NETWORK LOSS in the return audio stream.

WHY THIS EXISTS
---------------
Every instrument we had said the return path was healthy while the audio was audibly
broken: 0% packet loss, low ping jitter, hw_ptr advancing at ~48000/s, all gates green.
Raising the receive buffer from 250ms to 400ms to 600ms changed nothing, which is itself a
result — a jitter buffer repairs LATE audio and cannot repair audio that was never captured.

So the question is not "is the network dropping packets". It is:

    is audio MISSING BEFORE IT WAS EVER SENT?

RTP answers that directly, and it needs no decoding at all:

    sequence numbers contiguous + timestamp jumps  ->  the SOURCE lost audio
                                                       (the bridge's USB capture gapped)
    sequence numbers missing                       ->  the NETWORK lost packets

The RTP timestamp is derived from the captured audio's presentation time, so a gap in the
capture shows up as a timestamp that advances further than the packet's own duration while
the sequence numbering stays perfect. That is the exact signature of the dwc2 gadget problem
documented in raspberrypi/linux#5188 (~1ms capture gaps that ALSA never reports), and it
cannot be confused with anything the network did.

USE IT TO SETTLE ONE QUESTION AT A TIME
---------------------------------------
    python3 tools/rtp-probe.py --secs 30 --label "camera ON"
    ... turn the meeting camera off ...
    python3 tools/rtp-probe.py --secs 30 --label "camera OFF"

If source-gap milliseconds per second falls sharply with the camera off, the video gadget is
starving the audio gadget on the shared dwc2 controller, and the fix is to reduce periodic
endpoint pressure or move audio off that controller. If it does not change, video is not the
aggravator and the fix lies elsewhere.

Requires tcpdump (BPF) access. On macOS that is root, or membership of access_bpf — the
Wireshark ChmodBPF helper grants the latter.
"""
import argparse, collections, os, struct, subprocess, sys, tempfile

DLT_NULL, DLT_EN10MB, DLT_RAW = 0, 1, 12


def capture(secs, port, iface):
    """Record `secs` of UDP traffic on `port` to a pcap and return the path."""
    path = os.path.join(tempfile.mkdtemp(prefix="rtp-probe-"), "cap.pcap")
    cmd = ["tcpdump", "-i", iface, "-s", "0", "-w", path, "-U",
           "udp", "and", "port", str(port)]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd            # -n: never prompt inside a capture window
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        p.wait(timeout=secs)
        err = (p.stderr.read() or b"").decode("utf-8", "replace")
        raise SystemExit("  tcpdump exited early: %s" % err.strip()[:200])
    except subprocess.TimeoutExpired:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    return path


def rtp_packets(path):
    """Yield (arrival_seconds, seq, timestamp, ssrc, payload_len) for each RTP packet."""
    with open(path, "rb") as f:
        gh = f.read(24)
        if len(gh) < 24:
            return
        magic = struct.unpack("<I", gh[:4])[0]
        if magic in (0xA1B2C3D4, 0xA1B23C4D):
            end, nano = "<", magic == 0xA1B23C4D
        elif magic in (0xD4C3B2A1, 0x4D3CB2A1):
            end, nano = ">", magic == 0x4D3CB2A1
        else:
            raise SystemExit("  not a pcap file")
        linktype = struct.unpack(end + "I", gh[20:24])[0]
        while True:
            ph = f.read(16)
            if len(ph) < 16:
                return
            ts_s, ts_f, incl, _orig = struct.unpack(end + "IIII", ph)
            data = f.read(incl)
            if len(data) < incl:
                return
            t = ts_s + (ts_f / 1e9 if nano else ts_f / 1e6)

            # strip the link layer
            if linktype == DLT_NULL:
                off = 4
            elif linktype == DLT_EN10MB:
                if len(data) < 14:
                    continue
                off = 14
            elif linktype == DLT_RAW:
                off = 0
            else:
                off = 4
            ip = data[off:]
            if len(ip) < 20 or (ip[0] >> 4) != 4:
                continue                       # IPv4 only; the mesh delivers on loopback v4
            ihl = (ip[0] & 0x0F) * 4
            if ip[9] != 17:                    # UDP
                continue
            udp = ip[ihl:]
            if len(udp) < 8:
                continue
            rtp = udp[8:]
            if len(rtp) < 12 or (rtp[0] >> 6) != 2:
                continue                       # RTP version 2
            cc = rtp[0] & 0x0F
            hdr = 12 + 4 * cc
            if len(rtp) < hdr:
                continue
            seq, tstamp, ssrc = struct.unpack("!HII", rtp[2:12])
            yield t, seq, tstamp, ssrc, len(rtp) - hdr


def analyse(path, clock=48000):
    pkts = list(rtp_packets(path))
    if len(pkts) < 20:
        raise SystemExit("  only %d RTP packets captured — is the session live?" % len(pkts))

    # One stream only: the loopback carries both directions of nothing else, but be strict.
    ssrc = collections.Counter(p[3] for p in pkts).most_common(1)[0][0]
    pkts = [p for p in pkts if p[3] == ssrc]

    # The nominal timestamp step IS the packet duration; take the mode rather than assuming
    # 20ms, because the encoder's framing is a configuration choice we should not hardcode.
    deltas = []
    for (t0, s0, ts0, _, _), (t1, s1, ts1, _, _) in zip(pkts, pkts[1:]):
        if ((s1 - s0) & 0xFFFF) == 1:
            deltas.append((ts1 - ts0) & 0xFFFFFFFF)
    if not deltas:
        raise SystemExit("  no contiguous packet pairs — cannot establish framing")
    step = collections.Counter(deltas).most_common(1)[0][0]

    wall = pkts[-1][0] - pkts[0][0]
    lost = source_gaps = 0
    gap_ms_total = 0.0
    gap_sizes = []
    jitter = 0.0
    prev = None

    for (t0, s0, ts0, _, _), (t1, s1, ts1, _, _) in zip(pkts, pkts[1:]):
        dseq = (s1 - s0) & 0xFFFF
        dts = (ts1 - ts0) & 0xFFFFFFFF
        if dseq == 0:
            continue
        if dseq > 1:
            lost += dseq - 1                    # NETWORK: packets never arrived
        else:
            # SEQUENCE IS PERFECT. Any timestamp advance beyond one packet of audio means
            # the sender had nothing to send for that interval — the capture gapped.
            if dts > step:
                extra = dts - step
                ms = extra * 1000.0 / clock
                if ms >= 0.5:                   # ignore sub-half-ms framing wobble
                    source_gaps += 1
                    gap_ms_total += ms
                    gap_sizes.append(ms)
        # RFC 3550 interarrival jitter, in milliseconds
        d = abs((t1 - t0) * clock - dts) / clock * 1000.0
        jitter += (d - jitter) / 16.0
        prev = t1

    return {
        "packets": len(pkts), "wall": wall, "step_ms": step * 1000.0 / clock,
        "lost": lost, "loss_pct": 100.0 * lost / max(1, lost + len(pkts)),
        "source_gaps": source_gaps, "gap_ms_total": gap_ms_total,
        "gaps_per_s": source_gaps / wall if wall else 0,
        "gap_ms_per_s": gap_ms_total / wall if wall else 0,
        "worst_gap_ms": max(gap_sizes) if gap_sizes else 0.0,
        "jitter_ms": jitter,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=int, default=30)
    ap.add_argument("--port", type=int, default=5004)
    ap.add_argument("--iface", default="lo0")
    ap.add_argument("--label", default="")
    ap.add_argument("--pcap", default=None, help="analyse an existing capture instead")
    a = ap.parse_args()

    path = a.pcap or capture(a.secs, a.port, a.iface)
    r = analyse(path)

    print()
    print("  RTP probe%s" % (" — %s" % a.label if a.label else ""))
    print("  " + "-" * 58)
    print("  captured        %d packets over %.1fs (%.0fms per packet)"
          % (r["packets"], r["wall"], r["step_ms"]))
    print()
    print("  NETWORK loss    %d packets   %.2f%%" % (r["lost"], r["loss_pct"]))
    print("  arrival jitter  %.2f ms" % r["jitter_ms"])
    print()
    print("  SOURCE gaps     %d   (audio the bridge never captured)" % r["source_gaps"])
    print("    rate          %.2f gaps/s" % r["gaps_per_s"])
    print("    lost audio    %.1f ms total   %.2f ms per second of stream"
          % (r["gap_ms_total"], r["gap_ms_per_s"]))
    print("    worst single  %.1f ms" % r["worst_gap_ms"])
    print()
    # A buffer cannot repair what was never recorded, so the two columns imply different work.
    if r["source_gaps"] and r["gap_ms_per_s"] > r["jitter_ms"] / 10:
        print("  => The SOURCE is losing audio. No receive buffer can repair this;")
        print("     the fix has to be at the capture, not the network.")
    elif r["lost"]:
        print("  => Real network loss. FEC and buffer depth are the right levers.")
    else:
        print("  => Clean: no source gaps, no loss.")
    print()
    if not a.pcap:
        print("  capture kept at %s" % path)


if __name__ == "__main__":
    main()
