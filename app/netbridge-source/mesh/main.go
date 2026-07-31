// netbridge-mesh — the presenter app's EMBEDDED mesh client (walkthrough J3 phase 5:
// "joins the private mesh with an embedded client + scoped token from sign-in").
//
// It is a userspace Tailscale node (tsnet — no daemon, no `tailscale` install, no admin
// key). The Python app fetches a short-lived key from /auth/mesh-key at sign-in and hands
// it here; this process joins the tailnet as tag:source and proxies the media UDP between
// ffmpeg/gstreamer on 127.0.0.1 and the bridge across the mesh:
//
//   forward  ffmpeg  -> 127.0.0.1:5000/5002  --[tailnet]-->  bridge:5000/5002
//   return   bridge  --[tailnet]--> us:5004  ->  127.0.0.1:5004  gstreamer
//
// So the presenter installs NOTHING and never sees a 100.x address. Being tag:source, it
// is covered by the tailnet's existing tag:source->tag:bridge (and bridge->tag:source
// :5004) grants — no per-presenter ACL widening.
//
// On success it prints ONE json line to stdout and keeps running:
//   {"ready":true,"tailnet_ip":"100.x.y.z"}
// The app reads tailnet_ip to register the return-audio peer, then points ffmpeg at
// 127.0.0.1. The node is ephemeral: it self-removes from the tailnet when this exits.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"tailscale.com/tsnet"
)

// meshLogf routes tsnet's internal logs to stderr only when NB_MESH_DEBUG=1 (the app pipes
// our stderr to netbridge-source-mesh.log). Default stays quiet so stdout is our one
// handshake line and the log isn't spammed in normal use.
var meshDebug = os.Getenv("NB_MESH_DEBUG") == "1"

func meshLogf(f string, a ...any) {
	if meshDebug {
		fmt.Fprintf(os.Stderr, "[tsnet] "+f+"\n", a...)
	}
}

func dbg(f string, a ...any) {
	if meshDebug {
		fmt.Fprintf(os.Stderr, "[mesh] "+f+"\n", a...)
	}
}

func main() {
	authKey := flag.String("authkey", "", "scoped ephemeral tailnet key from /auth/mesh-key")
	hostname := flag.String("hostname", "netbridge-source", "tailnet node name")
	bridge := flag.String("bridge", "", "bridge tailnet IP or MagicDNS name")
	fwd := flag.String("forward", "5000,5002", "comma-sep UDP ports proxied local->bridge")
	ret := flag.Int("return", 5004, "UDP port proxied bridge->local (return audio)")
	// The app also talks to the bridge's HTTP control API (unlock / checks / set-peer /
	// status on :8080). To keep the presenter off any 100.x address entirely, that TCP
	// channel is proxied too: app -> 127.0.0.1:<local> -> [mesh] -> bridge:<remote>.
	ctrl := flag.String("control", "18080:8080", "TCP control proxy localPort:bridgePort")
	stateDir := flag.String("statedir", "", "tsnet state dir (temp if empty)")
	// /auth/mesh-key returns a login_server; default is tailscale.com, but a self-hosted
	// Headscale control plane needs it passed through or the node joins the WRONG network.
	control := flag.String("login-server", "", "control URL (blank = tailscale.com)")
	flag.Parse()

	if *authKey == "" || *bridge == "" {
		fatal("authkey and bridge are required")
	}

	dir := *stateDir
	if dir == "" {
		var err error
		dir, err = os.MkdirTemp("", "netbridge-mesh-")
		if err != nil {
			fatal("statedir: %v", err)
		}
		defer os.RemoveAll(dir)
	}

	s := &tsnet.Server{
		Hostname:     *hostname,
		AuthKey:      *authKey,
		Dir:          dir,
		Ephemeral:    true,                    // node auto-removes on disconnect
		ControlURL:   *control,                // "" => tailscale.com default
		Logf:         meshLogf,                // NB_MESH_DEBUG=1 -> stderr; else quiet
	}
	defer s.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	st, err := s.Up(ctx)
	if err != nil {
		fatal("could not join the mesh: %v", err)
	}
	ip4, _ := s.TailscaleIPs()
	if !ip4.IsValid() {
		if len(st.TailscaleIPs) > 0 {
			ip4 = st.TailscaleIPs[0]
		} else {
			fatal("joined the mesh but got no tailnet IP")
		}
	}

	// Bind every local port SYNCHRONOUSLY here, before the handshake. If a port is busy
	// (classically: a stale mesh helper from a previous session still holding it), fail
	// fast with a clear error instead of printing ready and then silently dropping the
	// media — that half-working state is what surfaced to the presenter as "unlock timed
	// out" with the video/voice checks stuck red.
	// forward legs: ffmpeg -> 127.0.0.1:port -> [mesh] -> bridge:port
	for _, ps := range strings.Split(*fwd, ",") {
		ps = strings.TrimSpace(ps)
		if ps == "" {
			continue
		}
		if err := forward(s, ps, *bridge); err != nil {
			fatal("forward %s: %v", ps, err)
		}
	}
	// return leg: bridge -> [mesh] us:ret -> 127.0.0.1:ret -> gstreamer
	if err := returnLeg(s, ip4.String(), *ret); err != nil {
		fatal("return audio: %v", err)
	}
	// control leg: app HTTP -> 127.0.0.1:local -> [mesh] -> bridge:remote
	var ctrlLocal string
	if lp, rp, ok := splitPorts(*ctrl); ok {
		ctrlLocal = lp
		if err := controlLeg(s, lp, rp, *bridge); err != nil {
			fatal("control: %v", err)
		}
	}

	// Handshake AFTER the proxies are wired, so the app never races us.
	out, _ := json.Marshal(map[string]any{
		"ready": true, "tailnet_ip": ip4.String(), "control_port": ctrlLocal})
	fmt.Println(string(out))
	os.Stdout.Sync()

	// Run until the app kills us (end session / quit). os.Interrupt is the portable Ctrl+C
	// on every OS; SIGTERM is added for Unix but is never delivered on Windows (there the
	// app's proc.terminate() is a hard TerminateProcess and this wait is moot). The node is
	// Ephemeral, so even a hard kill leaves the control plane to garbage-collect it.
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	<-sig
}

// forward binds a LOCAL udp socket ffmpeg sends to, and relays every datagram over the
// mesh to the bridge. One-directional: RTP out is a pure sender. The local bind happens
// synchronously so a busy port is reported to the caller (fail-fast) rather than swallowed.
func forward(s *tsnet.Server, port, bridge string) error {
	local, err := net.ListenPacket("udp", "127.0.0.1:"+port)
	if err != nil {
		return fmt.Errorf("local listen: %w", err)
	}
	// Dial the bridge over the mesh once; a UDP "conn" here is just an addressed sender.
	mesh, err := s.Dial(context.Background(), "udp", net.JoinHostPort(bridge, port))
	if err != nil {
		local.Close()
		return fmt.Errorf("mesh dial: %w", err)
	}
	go func() {
		defer local.Close()
		defer mesh.Close()
		buf := make([]byte, 1500)
		for {
			n, _, err := local.ReadFrom(buf)
			if err != nil {
				return
			}
			if _, err := mesh.Write(buf[:n]); err != nil {
				return
			}
		}
	}()
	return nil
}

// returnLeg receives return audio from the bridge on our MESH interface and hands it to
// gstreamer/ffmpeg on 127.0.0.1. The bridge was told (via set-peer) to send to our
// tailnet IP, so these packets arrive over the mesh.
//
// tsnet's ListenPacket requires a CONCRETE tailnet IP in the address — a bare ":5004"
// is rejected with "address must be a valid IP", which silently killed all return audio
// over the mesh. Bind on our own tailnet IP (tsip) explicitly.
func returnLeg(s *tsnet.Server, tsip string, port int) error {
	// Receive the return stream over the mesh on our tailnet IP and hand it to the local
	// player. REQUIRES tsnet >= v1.98 — older versions' netstack silently dropped inbound
	// UDP to a userspace ListenPacket, which made return audio dead over the embedded mesh
	// (verified: v1.80.3 drops, v1.98.9 receives the bridge's stream cross-machine).
	mesh, err := s.ListenPacket("udp", fmt.Sprintf("%s:%d", tsip, port))
	if err != nil {
		return fmt.Errorf("mesh listen: %w", err)
	}
	dbg("returnLeg listening on mesh %s:%d -> 127.0.0.1:%d", tsip, port, port)
	// Deliver to the local player over a CONNECTED socket: an unconnected WriteTo repeats a
	// route lookup for every single packet, and this leg carries ~50 packets/second of
	// real-time audio where per-packet work turns straight into audible jitter.
	//
	// A connected socket is what the ORIGINAL code used, and it caused the worst bug in this
	// file: the player's ICMP port-unreachable (arriving whenever gst was mid-restart) came
	// back as a write error, and the read loop did `return` on it — killing return audio for
	// the rest of the session. The fix is not to avoid connected sockets, it is to NEVER EXIT
	// on a write error. That is preserved below; the socket type is just the fast one now.
	dst, err := net.ResolveUDPAddr("udp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		mesh.Close()
		return fmt.Errorf("local addr: %w", err)
	}
	local, err := net.DialUDP("udp", nil, dst)
	if err != nil {
		mesh.Close()
		return fmt.Errorf("local socket: %w", err)
	}
	// Absorb bursts instead of dropping them. tsnet hands packets up from a userspace
	// netstack whose scheduling is not real-time; without headroom a momentary stall loses
	// audio outright.
	if uc, ok := mesh.(interface{ SetReadBuffer(int) error }); ok {
		_ = uc.SetReadBuffer(1 << 20)
	}
	_ = local.SetWriteBuffer(1 << 20)

	// DECOUPLE read from write. Previously this was one synchronous loop: no read was
	// pending while a write was in flight, so any hiccup in the local write let packets bunch
	// up in the netstack and then arrive in a burst — which is exactly what jitter sounds
	// like. A reader goroutine now only reads, a writer goroutine only writes, and a small
	// buffered channel joins them.
	type pkt struct {
		b []byte
		n int
	}
	ch := make(chan pkt, 64) // ~1.3 s of Opus at 50 pkt/s: enough to ride out a stall, small
	//                          enough that we can never add meaningful latency
	go func() {
		defer local.Close()
		var count uint64
		for p := range ch {
			count++
			if _, err := local.Write(p.b[:p.n]); err != nil {
				// Never surrender the leg on a transient local send error; the player may
				// be mid-restart (this is the 2026-07-29 bug — do not turn it into a return).
				if count%200 == 0 {
					dbg("returnLeg local write error after %d pkts: %v (continuing)", count, err)
				}
			}
		}
	}()
	go func() {
		defer mesh.Close()
		defer close(ch)
		var count, dropped uint64
		for {
			buf := make([]byte, 1500)
			n, from, err := mesh.ReadFrom(buf)
			if err != nil {
				dbg("returnLeg mesh.ReadFrom closed after %d pkts (%d dropped): %v",
					count, dropped, err)
				return
			}
			count++
			if count == 1 || count%200 == 0 {
				dbg("returnLeg RX #%d %d bytes from %v (dropped %d)", count, n, from, dropped)
			}
			select {
			case ch <- pkt{buf, n}:
			default:
				// Writer is wedged. DROP rather than block: stalling the reader would back
				// pressure into the netstack and convert a brief hiccup into a long burst.
				// For real-time audio a dropped packet is strictly better than a late one —
				// the jitterbuffer conceals a loss, it cannot undo added latency.
				dropped++
			}
		}
	}()
	return nil
}

// controlLeg accepts local TCP connections (the app's HTTP calls to the bridge) and
// splices each to a fresh mesh connection to the bridge's control port. A new backend
// conn per client keeps requests independent, which matters for the app's short,
// sequential control calls.
func controlLeg(s *tsnet.Server, localPort, remotePort, bridge string) error {
	ln, err := net.Listen("tcp", "127.0.0.1:"+localPort)
	if err != nil {
		return fmt.Errorf("local listen: %w", err)
	}
	go func() {
		defer ln.Close()
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(client net.Conn) {
				defer client.Close()
				back, err := s.Dial(context.Background(), "tcp", net.JoinHostPort(bridge, remotePort))
				if err != nil {
					return
				}
				defer back.Close()
				done := make(chan struct{}, 2)
				go func() { io.Copy(back, client); done <- struct{}{} }()
				go func() { io.Copy(client, back); done <- struct{}{} }()
				<-done
			}(c)
		}
	}()
	return nil
}

func splitPorts(s string) (local, remote string, ok bool) {
	i := strings.IndexByte(s, ':')
	if i <= 0 || i >= len(s)-1 {
		return "", "", false
	}
	return s[:i], s[i+1:], true
}

func fatal(f string, a ...any) {
	out, _ := json.Marshal(map[string]any{"ready": false, "error": fmt.Sprintf(f, a...)})
	fmt.Println(string(out))
	os.Exit(1)
}
