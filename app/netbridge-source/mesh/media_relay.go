package main

import (
	"context"
	"fmt"
	"net"
	"os"
	"time"
)

type mediaPacket struct {
	data     []byte
	received time.Time
}

// Keep reading while a single transport leg repairs. Queued media has a strict
// residence deadline; a recovered socket never drains seconds of old speech/video.
func relayForward(local net.PacketConn, mesh net.Conn, dial func(context.Context) (net.Conn, error)) {
	relayForwardPolicy(local, mesh, dial, [3]time.Duration{2 * time.Second, 5 * time.Second, 10 * time.Second}, 100*time.Millisecond)
}

func relayForwardPolicy(local net.PacketConn, mesh net.Conn, dial func(context.Context) (net.Conn, error), delays [3]time.Duration, maxAge time.Duration) {
	defer local.Close()
	defer func() {
		if mesh != nil {
			mesh.Close()
		}
	}()
	packets := make(chan mediaPacket, 64)
	done := make(chan struct{})
	defer close(done)
	go func() {
		defer close(packets)
		for {
			b := make([]byte, 1500)
			n, _, err := local.ReadFrom(b)
			if err != nil {
				return
			}
			p := mediaPacket{b[:n], time.Now()}
			select {
			case packets <- p:
			case <-done:
				return
			default: // Never block the reader behind failed transport.
			}
		}
	}()
	failures := 0
	var retryAt, healthySince time.Time
	for p := range packets {
		if time.Since(p.received) > maxAge {
			continue
		}
		if mesh == nil {
			if time.Now().Before(retryAt) {
				continue
			}
			if failures >= 3 {
				failures = 0
			} // one new burst after the circuit cooldown
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			next, err := dial(ctx)
			cancel()
			if err != nil {
				retryAt = time.Now().Add(delays[failures])
				failures++
				if failures == 3 {
					retryAt = time.Now().Add(time.Minute)
				}
				continue
			}
			failures++
			mesh = next
			// Discard the packet which triggered a possibly slow dial.
			continue
		}
		_ = mesh.SetWriteDeadline(time.Now().Add(maxAge))
		if _, err := mesh.Write(p.data); err != nil {
			mesh.Close()
			mesh = nil
			healthySince = time.Time{}
			if failures < 3 {
				retryAt = time.Now().Add(delays[failures])
			} else {
				retryAt = time.Now().Add(time.Minute)
			}
			fmt.Fprintln(os.Stderr, "Media transport unavailable; affected leg only")
			continue
		}
		if healthySince.IsZero() {
			healthySince = time.Now()
		}
		if time.Since(healthySince) >= time.Minute {
			failures = 0
		}
	}
}

// Rebind only the return socket when it fails. The running mesh identity and all
// other legs remain intact, including through a control-plane outage.
func receiveMedia(ctx context.Context, mesh net.PacketConn, dial func() (net.PacketConn, error), deliver func([]byte), delays [3]time.Duration, cooldown time.Duration) {
	defer func() {
		if mesh != nil {
			mesh.Close()
		}
	}()
	attempts := 0
	healthySince := time.Time{}
	for {
		if ctx.Err() != nil {
			return
		}
		if mesh == nil {
			wait := cooldown
			if attempts < len(delays) {
				wait = delays[attempts]
			}
			timer := time.NewTimer(wait)
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
			if attempts == len(delays) {
				attempts = 0
			}
			attempts++
			next, err := dial()
			if err != nil {
				continue
			}
			mesh = next
		}
		b := make([]byte, 1500)
		n, _, err := mesh.ReadFrom(b)
		if err != nil {
			mesh.Close()
			mesh = nil
			healthySince = time.Time{}
			continue
		}
		if healthySince.IsZero() {
			healthySince = time.Now()
		}
		if time.Since(healthySince) >= time.Minute {
			attempts = 0
		}
		deliver(b[:n])
	}
}
