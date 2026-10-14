package main

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func fixture() (Config, Public, Hint, []byte, time.Time) {
	now := time.Unix(2000, 0)
	c := Config{Node: "n2", Members: map[string]string{"n1": "10.10.10.11", "n2": "10.10.10.12", "n3": "10.10.10.13"}}
	p := Public{Protocol: "desk-vpn-ha-v1", Node: "n2", VIP: "192.168.1.254", Interface: "wlan0", Wireguard: "wgdesk0", ServerPublic: "server", Peers: map[string]string{"peer": "10.77.0.2/32"}}
	h := Hint{"desk-vpn-endpoints-v1", "n1", "boot", "server", now.UnixNano(), map[string]string{"peer": "8.8.8.8:12345"}}
	return c, p, h, []byte(strings.Repeat("a", 64)), now
}
func TestAuthenticatedHints(t *testing.T) {
	c, p, h, key, now := fixture()
	b, _ := sign(h, key)
	if _, e := decode(b, "10.10.10.11", c, p, key, now); e != nil {
		t.Fatal(e)
	}
	for _, src := range []string{"192.168.1.101", "10.10.10.13", "1.1.1.1"} {
		if _, e := decode(b, src, c, p, key, now); e == nil {
			t.Fatal("source accepted")
		}
	}
	if _, e := decode(b, "10.10.10.11", c, p, []byte("wrong"), now); e == nil {
		t.Fatal("bad MAC accepted")
	}
	var env Envelope
	json.Unmarshal(b, &env)
	env.Data = json.RawMessage(`{"Node":"forged"}`)
	b, _ = json.Marshal(env)
	if _, e := decode(b, "10.10.10.11", c, p, key, now); e == nil {
		t.Fatal("tamper accepted")
	}
}
func TestRejectStaleAndBadScope(t *testing.T) {
	c, p, h, key, now := fixture()
	for _, mutate := range []func(*Hint){func(v *Hint) { v.Sent = now.Add(-16 * time.Second).UnixNano() }, func(v *Hint) { v.Sent = now.Add(2 * time.Second).UnixNano() }, func(v *Hint) { v.Server = "other" }, func(v *Hint) { v.Peers = map[string]string{"unknown": "8.8.8.8:51820"} }, func(v *Hint) { v.Peers = map[string]string{"peer": "127.0.0.1:51820"} }} {
		v := h
		mutate(&v)
		b, _ := sign(v, key)
		if _, e := decode(b, "10.10.10.11", c, p, key, now); e == nil {
			t.Fatal("bad scope accepted")
		}
	}
}
func TestReplayExpiryConflict(t *testing.T) {
	_, _, h, _, now := fixture()
	c := Cache{last: map[string]int64{}, hints: map[string]Hint{}}
	if !c.store(h) || c.store(h) {
		t.Fatal("replay")
	}
	if _, ok := c.candidate(now); !ok {
		t.Fatal("fresh hint missing")
	}
	if _, ok := c.candidate(now.Add(16 * time.Second)); ok {
		t.Fatal("expired hint accepted")
	}
	h.Node = "n3"
	c.store(h)
	if _, ok := c.candidate(now); ok {
		t.Fatal("conflicting owner accepted")
	}
}
func TestEndpointAndAllowedIPs(t *testing.T) {
	for _, s := range []string{"127.0.0.1:1", "192.168.1.1:1", "[::1]:1", "8.8.8.8:0", "8.8.8.8:65536", "8.8.8.8:1;bad", "example.com:1"} {
		if endpoint(s) {
			t.Fatal(s)
		}
	}
	c, p, _, _, _ := fixture()
	if !validPublic(p, c.Node) {
		t.Fatal("contract")
	}
	p.Peers["peer"] = "192.168.1.1/32"
	if validPublic(p, c.Node) {
		t.Fatal("LAN peer address accepted")
	}
}
func TestRecoveryMutatesOnlyFreshOwnerAndRetainsIdentity(t *testing.T) {
	_, p, h, _, _ := fixture()
	calls := []string{}
	poke := func(ip string) { calls = append(calls, "poke "+ip) }
	psk := "(none)"
	keep := "off"
	stamp := "1000"
	allowedCIDR := "10.77.0.2/32"
	run := func(name string, args ...string) (string, error) {
		line := name + " " + strings.Join(args, " ")
		calls = append(calls, line)
		if len(args) == 3 && args[0] == "show" {
			switch args[2] {
			case "allowed-ips":
				return "peer\t" + allowedCIDR, nil
			case "latest-handshakes":
				return "peer\t" + stamp, nil
			case "preshared-keys":
				return "peer\t" + psk, nil
			case "persistent-keepalive":
				return "peer\t" + keep, nil
			}
		}
		return "", nil
	}
	if n := recoverPeers(p, h, 2000, func() bool { return false }, run, poke); n != 0 {
		t.Fatal(n)
	}
	for _, s := range calls {
		if strings.Contains(s, " set ") {
			t.Fatal("standby mutated peer")
		}
	}
	calls = nil
	if n := recoverPeers(p, h, 2000, func() bool { return true }, run, poke); n != 1 {
		t.Fatal(n)
	}
	joined := strings.Join(calls, "\n")
	if !strings.Contains(joined, "peer peer remove") || !strings.Contains(joined, "allowed-ips 10.77.0.2/32 endpoint 8.8.8.8:12345") || !strings.Contains(joined, "poke 10.77.0.2") {
		t.Fatal(joined)
	}
	if strings.Contains(joined, "private-key") {
		t.Fatal("identity touched")
	}
	for _, field := range []string{"fresh", "psk", "deleted", "changed"} {
		calls = nil
		stamp = "1000"
		psk = "(none)"
		keep = "off"
		allowedCIDR = "10.77.0.2/32"
		if field == "deleted" {
			allowedCIDR = ""
		}
		if field == "changed" {
			allowedCIDR = "10.77.0.3/32"
		}
		if field == "fresh" {
			stamp = "2000"
		}
		if field == "psk" {
			psk = "fixture-secret"
		}
		if field == "keepalive" {
			keep = "25"
		}
		if recoverPeers(p, h, 2000, func() bool { return true }, run, poke) != 0 {
			t.Fatal(field)
		}
		for _, s := range calls {
			if strings.Contains(s, " set ") {
				t.Fatal(field)
			}
		}
	}
}

func TestRecoveryPreservesPSKAndKeepaliveWithoutArguments(t *testing.T) {
	_, p, h, _, _ := fixture()
	secret := "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
	calls := []string{}
	run := func(name string, args ...string) (string, error) {
		if len(args) == 3 && args[0] == "show" {
			switch args[2] {
			case "latest-handshakes":
				return "peer\t1000", nil
			case "allowed-ips":
				return "peer\t10.77.0.2/32", nil
			case "preshared-keys":
				return "peer\t" + secret, nil
			case "persistent-keepalive":
				return "peer\t25", nil
			}
		}
		calls = append(calls, strings.Join(args, " "))
		return "", nil
	}
	secure := func(name string, args []string, input string) (string, error) {
		if input != secret+"\n" {
			t.Fatal("PSK changed")
		}
		calls = append(calls, strings.Join(args, " "))
		return "", nil
	}
	if recoverPeers(p, h, 2000, func() bool { return true }, run, func(string) {}, secure) != 1 {
		t.Fatal("PSK peer skipped")
	}
	all := strings.Join(calls, "\n")
	if strings.Contains(all, secret) || !strings.Contains(all, "preshared-key /dev/stdin") || !strings.Contains(all, "persistent-keepalive 25") {
		t.Fatal("PSK exposed or keepalive lost")
	}
}
