package main

import (
	"context"
	"errors"
	"net"
	"sync/atomic"
	"testing"
	"time"
)

type testMediaConn struct {
	net.Conn
	fail   bool
	writes chan []byte
}

func (c *testMediaConn) Close() error                     { return nil }
func (c *testMediaConn) SetWriteDeadline(time.Time) error { return nil }
func (c *testMediaConn) Write(p []byte) (int, error) {
	if c.fail {
		return 0, errors.New("injected transport fault")
	}
	c.writes <- append([]byte(nil), p...)
	return len(p), nil
}
func TestRelayRepairsOnlyItsSocketAndDropsDialBacklog(t *testing.T) {
	local, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	sender, err := net.Dial("udp", local.LocalAddr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer sender.Close()
	writes := make(chan []byte, 128)
	entered, release := make(chan struct{}), make(chan struct{})
	done := make(chan struct{})
	go func() {
		relayForwardPolicy(local, &testMediaConn{fail: true}, func(context.Context) (net.Conn, error) {
			close(entered)
			<-release
			return &testMediaConn{writes: writes}, nil
		}, [3]time.Duration{}, 20*time.Millisecond)
		close(done)
	}()
	defer func() {
		local.Close()
		select {
		case <-done:
		case <-time.After(time.Second):
			t.Error("relay did not stop")
		}
	}()
	sender.Write([]byte("fault"))
	time.Sleep(10 * time.Millisecond)
	sender.Write([]byte("trigger dial"))
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("did not repair")
	}
	sender.Write([]byte("stale speech"))
	time.Sleep(60 * time.Millisecond)
	close(release)
	time.Sleep(20 * time.Millisecond)
	sender.Write([]byte("fresh"))
	select {
	case got := <-writes:
		if string(got) != "fresh" {
			t.Fatalf("replayed %q", got)
		}
	case <-time.After(time.Second):
		t.Fatal("fresh packet lost")
	}
}
func TestRelayStopsAfterThreeRepairAttempts(t *testing.T) {
	local, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer local.Close()
	sender, err := net.Dial("udp", local.LocalAddr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer sender.Close()
	var attempts atomic.Int32
	done := make(chan struct{})
	go func() {
		relayForwardPolicy(local, &testMediaConn{fail: true}, func(context.Context) (net.Conn, error) { attempts.Add(1); return nil, errors.New("offline") }, [3]time.Duration{}, time.Second)
		close(done)
	}()
	for i := 0; i < 20; i++ {
		sender.Write([]byte("packet"))
		time.Sleep(5 * time.Millisecond)
	}
	local.Close()
	<-done
	if attempts.Load() != 3 {
		t.Fatalf("attempts=%d", attempts.Load())
	}
}

type readFailure struct{ net.PacketConn }

func (readFailure) ReadFrom([]byte) (int, net.Addr, error) { return 0, nil, errors.New("lost socket") }
func (readFailure) Close() error                           { return nil }
func TestReturnSocketRepairPreservesProcessAndBoundsRetries(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var attempts atomic.Int32
	done := make(chan struct{})
	go func() {
		receiveMedia(ctx, readFailure{}, func() (net.PacketConn, error) { attempts.Add(1); return nil, errors.New("offline") }, func([]byte) { t.Error("unexpected packet") }, [3]time.Duration{}, time.Hour)
		close(done)
	}()
	deadline := time.Now().Add(time.Second)
	for attempts.Load() < 3 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if attempts.Load() != 3 {
		t.Fatalf("attempts=%d", attempts.Load())
	}
	time.Sleep(10 * time.Millisecond)
	if attempts.Load() != 3 {
		t.Fatal("unbounded return socket retry loop")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("return repair did not cancel")
	}
}
