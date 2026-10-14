// Authenticated, ephemeral endpoint hints. Never synchronizes transport keys.
package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Config struct {
	Node    string
	Members map[string]string
	KeyFile string `json:"key_file"`
}
type Public struct {
	Protocol, Node, VIP, Interface, Wireguard string
	Peers                                     map[string]string
	ServerPublic                              string `json:"server_public"`
}
type Hint struct {
	Protocol, Node, Boot, Server string
	Sent                         int64
	Peers                        map[string]string
}
type Envelope struct {
	Data json.RawMessage
	MAC  string
}
type Cache struct {
	mu    sync.Mutex
	last  map[string]int64
	hints map[string]Hint
}

func bounded(path string, v interface{}) error {
	b, e := os.ReadFile(path)
	if e != nil {
		return e
	}
	if len(b) > 16384 {
		return errors.New("oversize configuration")
	}
	return json.Unmarshal(b, v)
}
func owned(p Public) bool {
	i, e := net.InterfaceByName(p.Interface)
	if e != nil {
		return false
	}
	as, e := i.Addrs()
	if e != nil {
		return false
	}
	for _, a := range as {
		ip, _, e := net.ParseCIDR(a.String())
		if e == nil && ip.String() == p.VIP {
			return true
		}
	}
	return false
}
func endpoint(s string) bool {
	host, port, e := net.SplitHostPort(s)
	if e != nil {
		return false
	}
	ip := net.ParseIP(host)
	n, e := strconv.Atoi(port)
	return e == nil && n > 0 && n <= 65535 && ip != nil && ip.To4() != nil && ip.IsGlobalUnicast() && !ip.IsPrivate() && !ip.IsLoopback() && !ip.IsLinkLocalUnicast() && host != "0.0.0.0"
}
func validPublic(p Public, node string) bool {
	if p.Protocol != "desk-vpn-ha-v1" || p.Node != node || p.VIP != "192.168.1.254" || p.Interface != "wlan0" || p.Wireguard != "wgdesk0" || p.ServerPublic == "" || len(p.Peers) > 32 {
		return false
	}
	for key, cidr := range p.Peers {
		ip, n, e := net.ParseCIDR(cidr)
		if e != nil || ip.To4() == nil || !strings.HasPrefix(ip.String(), "10.77.0.") || ip.String() == "10.77.0.1" || ip.String() == "10.77.0.0" || ip.String() == "10.77.0.255" || key == "" {
			return false
		}
		ones, bits := n.Mask.Size()
		if ones != 32 || bits != 32 {
			return false
		}
	}
	return true
}
func sign(h Hint, key []byte) ([]byte, error) {
	b, e := json.Marshal(h)
	if e != nil {
		return nil, e
	}
	m := hmac.New(sha256.New, key)
	m.Write(b)
	return json.Marshal(Envelope{b, hex.EncodeToString(m.Sum(nil))})
}
func decode(b []byte, source string, c Config, p Public, key []byte, now time.Time) (Hint, error) {
	var env Envelope
	var h Hint
	bad := errors.New("invalid hint")
	if len(b) > 16384 || json.Unmarshal(b, &env) != nil {
		return h, bad
	}
	sig, e := hex.DecodeString(env.MAC)
	if e != nil {
		return h, bad
	}
	mac := hmac.New(sha256.New, key)
	mac.Write(env.Data)
	if !hmac.Equal(sig, mac.Sum(nil)) || json.Unmarshal(env.Data, &h) != nil {
		return h, bad
	}
	age := now.Sub(time.Unix(0, h.Sent))
	if h.Protocol != "desk-vpn-endpoints-v1" || h.Node == c.Node || c.Members[h.Node] != source || h.Boot == "" || h.Server != p.ServerPublic || age < -time.Second || age > 15*time.Second || len(h.Peers) > len(p.Peers) {
		return h, bad
	}
	for peer, addr := range h.Peers {
		if _, ok := p.Peers[peer]; !ok || !endpoint(addr) {
			return h, bad
		}
	}
	return h, nil
}
func (c *Cache) store(h Hint) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if h.Sent <= c.last[h.Node] {
		return false
	}
	c.last[h.Node] = h.Sent
	c.hints[h.Node] = h
	return true
}
func (c *Cache) candidate(now time.Time) (Hint, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	var found Hint
	count := 0
	for _, h := range c.hints {
		age := now.Sub(time.Unix(0, h.Sent))
		if age >= -time.Second && age <= 15*time.Second {
			found = h
			count++
		}
	}
	return found, count == 1 && len(found.Peers) > 0
}
func command(name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	b, e := exec.CommandContext(ctx, name, args...).Output()
	if len(b) > 16384 {
		return "", errors.New("oversize runtime output")
	}
	return strings.TrimSpace(string(b)), e
}
func fields(text string) map[string]string {
	out := map[string]string{}
	for _, line := range strings.Split(text, "\n") {
		f := strings.Fields(line)
		if len(f) == 2 {
			out[f[0]] = f[1]
		}
	}
	return out
}
func hints(p Public, now time.Time) (map[string]string, error) {
	end, e := command("/usr/bin/wg", "show", p.Wireguard, "endpoints")
	if e != nil {
		return nil, e
	}
	stamps, e := command("/usr/bin/wg", "show", p.Wireguard, "latest-handshakes")
	if e != nil {
		return nil, e
	}
	hs := fields(stamps)
	out := map[string]string{}
	for peer, addr := range fields(end) {
		if _, ok := p.Peers[peer]; !ok {
			continue
		}
		stamp, e := strconv.ParseInt(hs[peer], 10, 64)
		age := now.Unix() - stamp
		if e == nil && stamp > 0 && age >= 0 && age <= 180 && endpoint(addr) {
			out[peer] = addr
		}
	}
	return out, nil
}
func routeReady(p Public) bool {
	s, e := command("/usr/sbin/ip", "-4", "route", "get", "1.1.1.1", "mark", "51853")
	if e != nil || !strings.Contains(s, "src "+p.VIP) || !strings.Contains(s, "table 51853") {
		return false
	}
	_, e = command("/usr/sbin/iptables", "-w", "1", "-C", "DESK_WG_OUTPUT", "-j", "ACCEPT")
	return e == nil
}

// Invoke only after a false->true VIP transition with a fresh single-owner hint.
func recoverPeers(p Public, h Hint, acquired int64, owns func() bool, run func(string, ...string) (string, error), poke func(string), secure ...func(string, []string, string) (string, error)) int {
	text, e := run("/usr/bin/wg", "show", p.Wireguard, "latest-handshakes")
	if e != nil {
		return 0
	}
	stamps := fields(text)
	count := 0
	allowedText, e := run("/usr/bin/wg", "show", p.Wireguard, "allowed-ips")
	if e != nil {
		return 0
	}
	allowed := fields(allowedText)
	pskText, e := run("/usr/bin/wg", "show", p.Wireguard, "preshared-keys")
	if e != nil {
		return 0
	}
	psks := fields(pskText)
	keepText, e := run("/usr/bin/wg", "show", p.Wireguard, "persistent-keepalive")
	if e != nil {
		return 0
	}
	keep := fields(keepText)
	for peer, addr := range h.Peers {
		cidr, ok := p.Peers[peer]
		if !ok || allowed[peer] != cidr || !endpoint(addr) || !owns() {
			continue
		}
		psk := psks[peer]
		ka := keep[peer]
		if psk != "(none)" {
			b, e := base64.StdEncoding.DecodeString(psk)
			if e != nil || len(b) != 32 {
				continue
			}
		}
		if ka != "off" {
			n, e := strconv.Atoi(ka)
			if e != nil || n < 0 || n > 65535 {
				continue
			}
		}
		stamp, _ := strconv.ParseInt(stamps[peer], 10, 64)
		if stamp >= acquired {
			continue
		}
		// Re-add the exact peer, preserving configured PSK and keepalive in RAM only.
		if _, e = run("/usr/bin/wg", "set", p.Wireguard, "peer", peer, "remove"); e != nil {
			continue
		}
		args := []string{"set", p.Wireguard, "peer", peer, "allowed-ips", cidr, "endpoint", addr}
		if ka != "off" && ka != "0" {
			args = append(args, "persistent-keepalive", ka)
		}
		configure := func() (string, error) { return run("/usr/bin/wg", args...) }
		if psk != "(none)" {
			args = append(args, "preshared-key", "/dev/stdin")
			fn := secureCommand
			if len(secure) > 0 {
				fn = secure[0]
			}
			configure = func() (string, error) { return fn("/usr/bin/wg", args, psk+"\n") }
		}
		if _, e = configure(); e != nil {
			configure()
			continue
		}
		if !owns() {
			continue
		}
		ip, _, _ := net.ParseCIDR(cidr)
		poke(ip.String())
		count++
	}
	return count
}

// One discard-port datagram through the prepared tunnel triggers the standard
// kernel handshake. No raw sockets, user agent or persistent keepalive is needed.
func poke(ip string) {
	c, e := net.DialTimeout("udp4", net.JoinHostPort(ip, "9"), 500*time.Millisecond)
	if e != nil {
		return
	}
	defer c.Close()
	c.SetWriteDeadline(time.Now().Add(500 * time.Millisecond))
	c.Write([]byte{0})
}
func secureCommand(name string, args []string, input string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdin = strings.NewReader(input)
	b, e := cmd.Output()
	if len(b) > 16384 {
		return "", errors.New("oversize runtime output")
	}
	return strings.TrimSpace(string(b)), e
}
func main() {
	log.SetFlags(0)
	var c Config
	var p Public
	if len(os.Args) != 2 || bounded(os.Args[1], &c) != nil || bounded("/etc/desk-vpn/public.json", &p) != nil || !validPublic(p, c.Node) || len(c.Members) != 3 {
		log.Fatal("invalid recovery configuration")
	}
	for _, ip := range c.Members {
		v := net.ParseIP(ip)
		if v == nil || v.To4() == nil || !strings.HasPrefix(ip, "10.10.10.") {
			log.Fatal("invalid private membership")
		}
	}
	key, e := os.ReadFile(c.KeyFile)
	key = bytes.TrimSpace(key)
	if e != nil || len(key) != 64 {
		log.Fatal("invalid recovery credential")
	}
	boot, e := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if e != nil {
		log.Fatal("boot identity unavailable")
	}
	addr, e := net.ResolveUDPAddr("udp4", net.JoinHostPort(c.Members[c.Node], "51854"))
	if e != nil {
		log.Fatal("invalid local membership")
	}
	conn, e := net.ListenUDP("udp4", addr)
	if e != nil {
		log.Fatal("private endpoint listener unavailable")
	}
	defer conn.Close()
	cache := &Cache{last: map[string]int64{}, hints: map[string]Hint{}}
	go func() {
		buf := make([]byte, 16385)
		for {
			n, from, e := conn.ReadFromUDP(buf)
			if e != nil {
				return
			}
			if from.Port != 51854 {
				continue
			}
			var current Public
			if bounded("/etc/desk-vpn/public.json", &current) != nil || !validPublic(current, c.Node) {
				continue
			}
			h, e := decode(buf[:n], from.IP.String(), c, current, key, time.Now())
			if e == nil {
				cache.store(h)
			}
		}
	}()
	// Starting/restarting this helper on an existing owner never resets a live peer.
	wasOwner := owned(p)
	var acquired time.Time
	pending := false
	lastSend := time.Time{}
	log.Println("VPN endpoint recovery ready; private metadata only")
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for now := range ticker.C {
		if bounded("/etc/desk-vpn/public.json", &p) != nil || !validPublic(p, c.Node) {
			continue
		}
		owner := owned(p)
		if owner && !wasOwner {
			acquired = now
			pending = true
		}
		if !owner {
			pending = false
		}
		wasOwner = owner
		if pending && now.Sub(acquired) > 10*time.Second {
			pending = false
			log.Println("VPN recovery skipped; normal client renewal retained")
		}
		if pending && routeReady(p) {
			if h, ok := cache.candidate(now); ok {
				n := recoverPeers(p, h, acquired.Unix(), func() bool { return owned(p) }, command, poke)
				pending = false
				log.Printf("VPN recovery initiated for %d configured peer(s)", n)
			}
		}
		if owner && now.Sub(lastSend) >= 5*time.Second {
			lastSend = now
			peers, e := hints(p, now)
			if e != nil || !owned(p) {
				continue
			}
			h := Hint{"desk-vpn-endpoints-v1", c.Node, strings.TrimSpace(string(boot)), p.ServerPublic, now.UnixNano(), peers}
			b, e := sign(h, key)
			if e != nil {
				continue
			}
			for node, ip := range c.Members {
				if node == c.Node {
					continue
				}
				target, e := net.ResolveUDPAddr("udp4", net.JoinHostPort(ip, "51854"))
				if e == nil {
					conn.WriteToUDP(b, target)
				}
			}
		}
	}
}
