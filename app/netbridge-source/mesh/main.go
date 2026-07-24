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
	"log"
	"net"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"tailscale.com/tsnet"
)

func main() {
	authKey := flag.String("authkey", "", "scoped ephemeral tailnet key from /auth/mesh-key")
	hostname := flag.String("hostname", "netbridge-source", "tailnet node name")
	bridge := flag.String("bridge", "", "bridge tailnet IP or MagicDNS name")
	fwd := flag.String("forward", "5000,5002", "comma-sep UDP ports proxied local->bridge")
	ret := flag.Int("return", 5004, "UDP port proxied bridge->local (return audio)")
	stateDir := flag.String("statedir", "", "tsnet state dir (temp if empty)")
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
		Hostname:  *hostname,
		AuthKey:   *authKey,
		Dir:       dir,
		Ephemeral: true,                     // node auto-removes on disconnect
		Logf:      func(string, ...any) {},  // quiet: stdout is our one json handshake line
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

	// forward legs: ffmpeg -> 127.0.0.1:port -> [mesh] -> bridge:port
	for _, ps := range strings.Split(*fwd, ",") {
		ps = strings.TrimSpace(ps)
		if ps == "" {
			continue
		}
		go forward(s, ps, *bridge)
	}
	// return leg: bridge -> [mesh] us:ret -> 127.0.0.1:ret -> gstreamer
	go returnLeg(s, *ret)

	// Handshake AFTER the proxies are wired, so the app never races us.
	out, _ := json.Marshal(map[string]any{"ready": true, "tailnet_ip": ip4.String()})
	fmt.Println(string(out))
	os.Stdout.Sync()

	// Run until the app kills us (end session / quit).
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
}

// forward binds a LOCAL udp socket ffmpeg sends to, and relays every datagram over the
// mesh to the bridge. One-directional: RTP out is a pure sender.
func forward(s *tsnet.Server, port, bridge string) {
	local, err := net.ListenPacket("udp", "127.0.0.1:"+port)
	if err != nil {
		log.Printf("forward %s: local listen: %v", port, err)
		return
	}
	defer local.Close()

	// Dial the bridge over the mesh once; a UDP "conn" here is just an addressed sender.
	mesh, err := s.Dial(context.Background(), "udp", net.JoinHostPort(bridge, port))
	if err != nil {
		log.Printf("forward %s: mesh dial: %v", port, err)
		return
	}
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
}

// returnLeg receives return audio from the bridge on our MESH interface and hands it to
// gstreamer/ffmpeg on 127.0.0.1. The bridge was told (via set-peer) to send to our
// tailnet IP, so these packets arrive over the mesh.
func returnLeg(s *tsnet.Server, port int) {
	mesh, err := s.ListenPacket("udp", fmt.Sprintf(":%d", port))
	if err != nil {
		log.Printf("return: mesh listen: %v", err)
		return
	}
	defer mesh.Close()

	local, err := net.Dial("udp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		log.Printf("return: local dial: %v", err)
		return
	}
	defer local.Close()

	buf := make([]byte, 1500)
	for {
		n, _, err := mesh.ReadFrom(buf)
		if err != nil {
			return
		}
		if _, err := local.Write(buf[:n]); err != nil {
			return
		}
	}
}

func fatal(f string, a ...any) {
	out, _ := json.Marshal(map[string]any{"ready": false, "error": fmt.Sprintf(f, a...)})
	fmt.Println(string(out))
	os.Exit(1)
}
