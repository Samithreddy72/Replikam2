#!/usr/bin/env python3
"""UDP helper for the real-kernel gate test.

  udp.py recv                      log every datagram on 5000/5002/5004 as "SRC PORT PAYLOAD"
  udp.py send SRC DST PORT TAG     send one datagram carrying TAG, from address SRC
"""
import select, socket, sys


def recv():
    socks = []
    for port in (5000, 5002, 5004):
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)     # IPv4 arrives as ::ffff:a.b.c.d
        s.bind(("::", port))
        socks.append(s)
    print("ready", flush=True)
    while True:
        ready, _, _ = select.select(socks, [], [])
        for s in ready:
            data, addr = s.recvfrom(2048)
            src = addr[0][7:] if addr[0].startswith("::ffff:") else addr[0]
            print("%s %d %s" % (src, s.getsockname()[1], data.decode("ascii", "replace")), flush=True)


def send(src, dst, port, tag):
    s = socket.socket(socket.AF_INET6 if ":" in src else socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((src, 0))
    s.sendto(tag.encode(), (dst, int(port)))


if __name__ == "__main__":
    if sys.argv[1] == "recv":
        recv()
    else:
        send(*sys.argv[2:6])
