// Package proxyv2 rejects missing/v1 PROXY headers before Caddy's standard parser.
package proxyv2

import (
	"bufio"
	"bytes"
	"errors"
	"net"
	"sync"

	"github.com/caddyserver/caddy/v2"
	"github.com/caddyserver/caddy/v2/caddyconfig/caddyfile"
)

func init() { caddy.RegisterModule(Guard{}) }

// Guard must precede proxy_protocol (which enforces the peer allowlist), then tls.
// The standard parser accepts v1 and v2; its allowlist alone cannot require v2.
type Guard struct{}

func (Guard) CaddyModule() caddy.ModuleInfo {
	return caddy.ModuleInfo{ID: "caddy.listeners.desk_proxy_v2", New: func() caddy.Module { return new(Guard) }}
}

func (Guard) UnmarshalCaddyfile(d *caddyfile.Dispenser) error {
	d.Next()
	if d.NextArg() || d.NextBlock(0) {
		return d.Err("desk_proxy_v2 takes no arguments")
	}
	return nil
}

func (Guard) WrapListener(l net.Listener) net.Listener { return &listener{l} }

type listener struct{ net.Listener }

func (l *listener) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	// Read in the connection's TLS goroutine, never in the shared accept loop.
	return &guarded{Conn: c, reader: bufio.NewReader(c)}, nil
}

type guarded struct {
	net.Conn
	reader *bufio.Reader
	once   sync.Once
	err    error
}

var signature = []byte("\r\n\r\n\x00\r\nQUIT\n")

func (c *guarded) Read(p []byte) (int, error) {
	c.once.Do(func() {
		// The outer standard parser owns the configured header deadline. Do not
		// clear it here: a partial v2 payload must retain that same deadline.
		header, err := c.reader.Peek(16)
		if err != nil {
			c.err = err
		} else if !bytes.Equal(header[:12], signature) ||
			(header[12] != 0x20 && header[12] != 0x21) {
			c.err = errors.New("PROXY v2 required")
		}
		if c.err != nil {
			_ = c.Conn.Close()
			return
		}
	})
	if c.err != nil {
		return 0, c.err
	}
	return c.reader.Read(p)
}

var _ caddy.ListenerWrapper = Guard{}
var _ caddyfile.Unmarshaler = (*Guard)(nil)
