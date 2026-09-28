package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestControlProxyRefusesBrowserBeforeDial(t *testing.T) {
	cases := []struct {
		name, method, host, key, value, body string
		status                               int
	}{
		{"rebind", "GET", "evil.example:18080", "", "", "", 403},
		{"origin", "POST", "127.0.0.1:18080", "Origin", "https://evil.example", "{}", 403},
		{"null-origin", "POST", "127.0.0.1:18080", "Origin", "null", "{}", 403},
		{"fetch", "GET", "127.0.0.1:18080", "Sec-Fetch-Site", "cross-site", "", 403},
		{"navigation", "GET", "127.0.0.1:18080", "Sec-Fetch-Mode", "navigate", "", 403},
		{"referer", "GET", "127.0.0.1:18080", "Referer", "https://evil.example", "", 403},
		{"preflight", "OPTIONS", "127.0.0.1:18080", "", "", "", 405},
		{"simple-post", "POST", "127.0.0.1:18080", "Content-Type", "text/plain", "{}", 415},
		{"form", "POST", "127.0.0.1:18080", "Content-Type", "application/x-www-form-urlencoded", "pin=1234", 415},
		{"large", "POST", "127.0.0.1:18080", "", "", strings.Repeat("x", 16385), 413},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dials := 0
			h := controlProxy("18080", "bridge:8080", func(context.Context, string, string) (net.Conn, error) {
				dials++
				return nil, fmt.Errorf("unexpected dial")
			})
			r := httptest.NewRequest(tc.method, "/api/unlock", strings.NewReader(tc.body))
			r.Host = tc.host
			r.Header.Set("Content-Type", "application/json")
			if tc.key != "" {
				r.Header.Set(tc.key, tc.value)
			}
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != tc.status || dials != 0 {
				t.Fatalf("status=%d dials=%d", w.Code, dials)
			}
		})
	}
}

func TestControlProxyNativeRoundTrip(t *testing.T) {
	for _, method := range []string{"GET", "POST"} {
		t.Run(method, func(t *testing.T) {
			calls := 0
			backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				b, _ := io.ReadAll(r.Body)
				if r.Method != method || r.URL.RequestURI() != "/api/unlock?check=1" || r.Host != "bridge:8080" || string(b) != "{}" {
					t.Errorf("incorrect forwarded request: %s %s %s %s", r.Method, r.URL, r.Host, b)
				}
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(401)
				fmt.Fprint(w, `{"reason":"wrong_pin"}`)
			}))
			defer backend.Close()
			h := controlProxy("18080", "bridge:8080", func(ctx context.Context, network, target string) (net.Conn, error) {
				if target != "bridge:8080" {
					t.Errorf("unexpected target %s", target)
				}
				return (&net.Dialer{}).DialContext(ctx, network, strings.TrimPrefix(backend.URL, "http://"))
			})
			r := httptest.NewRequest(method, "/api/unlock?check=1", strings.NewReader("{}"))
			r.Host = "127.0.0.1:18080"
			r.Header.Set("Content-Type", "application/json")
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != 401 || w.Body.String() != `{"reason":"wrong_pin"}` || calls != 1 {
				t.Fatalf("status=%d body=%s calls=%d", w.Code, w.Body, calls)
			}
		})
	}
}

func TestControlProxyDoesNotRetryLostPINResponse(t *testing.T) {
	dials := 0
	h := controlProxy("18080", "bridge:8080", func(context.Context, string, string) (net.Conn, error) {
		dials++
		client, server := net.Pipe()
		go func() {
			defer server.Close()
			req, err := http.ReadRequest(bufio.NewReader(server))
			if err == nil {
				io.Copy(io.Discard, req.Body)
				req.Body.Close()
			}
		}()
		return client, nil
	})
	r := httptest.NewRequest("POST", "/api/unlock", strings.NewReader(`{"pin":"1234"}`))
	r.Host = "127.0.0.1:18080"
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 502 || dials != 1 {
		t.Fatalf("status=%d dials=%d", w.Code, dials)
	}
}
