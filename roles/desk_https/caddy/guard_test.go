package proxyv2

import (
	"bufio"
	"crypto/tls"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/caddyserver/caddy/v2"
	standard "github.com/caddyserver/caddy/v2/modules/caddyhttp/proxyprotocol"
)

func v2(local bool) []byte {
	if local {
		return append(append([]byte{}, signature...), 0x20, 0, 0, 0)
	}
	return append(append([]byte{}, signature...), 0x21, 0x11, 0, 12, 198, 51, 100, 20, 127, 0, 0, 1, 0xc0, 0x00, 0x20, 0xfb)
}

func server(t *testing.T, allowed string) *httptest.Server {
	t.Helper()
	s := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, r.RemoteAddr) }))
	var parser standard.ListenerWrapper
	if err := json.Unmarshal([]byte(`{"allow":["`+allowed+`"],"fallback_policy":"reject","timeout":200000000}`), &parser); err != nil {
		t.Fatal(err)
	}
	if err := parser.Provision(caddy.Context{}); err != nil {
		t.Fatal(err)
	}
	s.Listener = parser.WrapListener(Guard{}.WrapListener(s.Listener))
	s.StartTLS()
	t.Cleanup(s.Close)
	return s
}

func connection(t *testing.T, s *httptest.Server, prefix []byte) (*tls.Conn, error) {
	t.Helper()
	c, err := net.DialTimeout("tcp", s.Listener.Addr().String(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	_ = c.SetDeadline(time.Now().Add(time.Second))
	if len(prefix) > 0 {
		if _, err = c.Write(prefix); err != nil {
			c.Close()
			return nil, err
		}
	}
	// The certificate belongs to this ephemeral test server, never production.
	client := tls.Client(c, &tls.Config{InsecureSkipVerify: true})
	if err = client.Handshake(); err != nil {
		c.Close()
		return nil, err
	}
	return client, nil
}

func TestDataAndHealthChecksRequireV2FromTrustedPeers(t *testing.T) {
	s := server(t, "127.0.0.1/32")
	for _, local := range []bool{false, true} {
		c, err := connection(t, s, v2(local))
		if err != nil {
			t.Fatal(err)
		}
		_, _ = io.WriteString(c, "GET / HTTP/1.1\r\nHost: cluster.huynm.dev\r\nConnection: close\r\n\r\n")
		r, err := http.ReadResponse(bufio.NewReader(c), nil)
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(r.Body)
		r.Body.Close()
		c.Close()
		want := "198.51.100.20:"
		if local {
			want = "127.0.0.1:"
		}
		if !strings.HasPrefix(string(body), want) {
			t.Fatalf("client identity: %s", body)
		}
	}
	for name, prefix := range map[string][]byte{"missing": nil, "v1": []byte("PROXY TCP4 198.51.100.20 127.0.0.1 49152 8443\r\n"), "bad-command": append(append([]byte{}, signature...), 0x22, 0, 0, 0), "partial": append(append([]byte{}, signature...), 0x21, 0x11, 0, 12)} {
		t.Run(name, func(t *testing.T) {
			c, err := connection(t, s, prefix)
			if err == nil {
				c.Close()
				t.Fatal("invalid PROXY accepted")
			}
		})
	}
	untrusted := server(t, "192.0.2.1/32")
	for _, local := range []bool{false, true} {
		if c, err := connection(t, untrusted, v2(local)); err == nil {
			c.Close()
			t.Fatal("untrusted peer accepted")
		}
	}
}
