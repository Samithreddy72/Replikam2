package main

import (
	"bytes"
	"context"
	"io"
	"mime"
	"net"
	"net/http"
	"net/http/httputil"
	"time"
)

// The native engine uses JSON over loopback. No browser needs direct access to
// this port: its UI talks to the separately guarded Python/Studio API.
func controlProxy(port, target string, dial func(context.Context, string, string) (net.Conn, error)) http.Handler {
	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
			ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
			defer cancel()
			return dial(ctx, "tcp", target)
		},
		DisableKeepAlives:     true, // never transparently replay a PIN write
		ResponseHeaderTimeout: 20 * time.Second,
	}
	proxy := &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.Out.URL.Scheme = "http"
			r.Out.URL.Host = target
			r.Out.Host = target
		},
		Transport: transport,
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			http.Error(w, "bridge control unavailable", http.StatusBadGateway)
		},
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Host != net.JoinHostPort("127.0.0.1", port) || r.URL.IsAbs() {
			http.Error(w, "invalid control host", http.StatusForbidden)
			return
		}
		for _, key := range []string{"Origin", "Referer", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Upgrade"} {
			if _, present := r.Header[key]; present {
				http.Error(w, "browser control access refused", http.StatusForbidden)
				return
			}
		}
		if r.Method != http.MethodGet && r.Method != http.MethodPost {
			http.Error(w, "unsupported control method", http.StatusMethodNotAllowed)
			return
		}
		if r.Method == http.MethodPost {
			kind, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
			if err != nil || kind != "application/json" || len(r.Header.Values("Content-Type")) != 1 {
				http.Error(w, "JSON required", http.StatusUnsupportedMediaType)
				return
			}
		}
		// Bound and fully read before dialing, including chunked/unknown lengths.
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 16<<10))
		if err != nil {
			http.Error(w, "invalid or oversized control body", http.StatusRequestEntityTooLarge)
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		r.ContentLength = int64(len(body))
		r.TransferEncoding = nil
		proxy.ServeHTTP(w, r)
	})
}
