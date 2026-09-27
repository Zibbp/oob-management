package main

import (
	"bufio"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// The W5500 driver's Accept can wedge the listen socket on transient errors
// (e.g. a client that handshakes then disconnects before Accept runs), and
// the stock http server treats any Accept error as fatal. So we run our own
// accept loop that heals the listener instead of dying, and serve each
// connection through the standard mux with a small in-RAM response adapter.

const (
	connDeadline   = 20 * time.Second
	maxConsecErrs  = 10
	listenRetryGap = time.Second
	acceptRetryGap = 100 * time.Millisecond
)

// resp buffers one response in RAM. All our bodies are small (UI ~10KB,
// JSON/text tiny); request bodies are never buffered, they stream.
type resp struct {
	hdr  http.Header
	body []byte
	code int
}

func (w *resp) Header() http.Header { return w.hdr }
func (w *resp) Write(b []byte) (int, error) {
	if w.code == 0 {
		w.code = http.StatusOK
	}
	w.body = append(w.body, b...)
	return len(b), nil
}
func (w *resp) WriteHeader(code int) {
	if w.code == 0 {
		w.code = code
	}
}

func serveConn(mux *http.ServeMux, c net.Conn) {
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(connDeadline))
	req, err := http.ReadRequest(bufio.NewReader(c))
	if err != nil {
		return
	}
	w := &resp{hdr: make(http.Header)}
	mux.ServeHTTP(w, req)
	if w.code == 0 {
		w.code = http.StatusOK
	}
	if w.hdr.Get("Content-Type") == "" {
		w.hdr.Set("Content-Type", "text/plain; charset=utf-8")
	}
	w.hdr.Set("Content-Length", strconv.Itoa(len(w.body)))
	w.hdr.Set("Connection", "close")
	if w.hdr.Get("Cache-Control") == "" {
		w.hdr.Set("Cache-Control", "no-store")
	}
	fmt.Fprintf(c, "HTTP/1.1 %d %s\r\n", w.code, http.StatusText(w.code))
	for k, vs := range w.hdr {
		for _, v := range vs {
			fmt.Fprintf(c, "%s: %s\r\n", k, v)
		}
	}
	c.Write([]byte("\r\n"))
	if len(w.body) > 0 {
		c.Write(w.body)
	}
}

// serveForever listens on :80 and never returns. A wedged listener is
// closed and re-created; per-connection failures stay per-connection.
func serveForever(mux *http.ServeMux) {
	for {
		ln, err := net.Listen("tcp", HTTPPort)
		if err != nil {
			logAdd("listen failed: %s; retry", err)
			time.Sleep(listenRetryGap)
			continue
		}
		logAdd("listening on http://%s%s/", currentIP, HTTPPort)
		broken := false
		for consec := 0; !broken; {
			c, err := ln.Accept()
			if err != nil {
				// Transient under burst load: all sockets busy serving.
				// Not a wedged listener, so don't count it toward healing.
				if strings.Contains(err.Error(), "No more sockets") {
					time.Sleep(50 * time.Millisecond)
					continue
				}
				consec++
				logAdd("accept err %d/%d: %s", consec, maxConsecErrs, err)
				time.Sleep(acceptRetryGap)
				if consec >= maxConsecErrs {
					broken = true
				}
				continue
			}
			consec = 0
			go serveConn(mux, c)
		}
		ln.Close()
		logAdd("listener healed, re-listening")
	}
}
