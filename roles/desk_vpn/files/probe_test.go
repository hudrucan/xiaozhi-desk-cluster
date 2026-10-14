package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestMaintenance(t *testing.T) {
	p := filepath.Join(t.TempDir(), "state.json")
	if paused("deskb1x", "boot", 100, p) {
		t.Fatal("missing marker")
	}
	for _, v := range []struct {
		body string
		want bool
	}{{`{"protocol":"desk-node-maintenance-v1","node":"deskb1x","boot_id":"boot","expires":101}`, true}, {`{"protocol":"desk-node-maintenance-v1","node":"deskb1x","boot_id":"boot","expires":99}`, false}, {`{"protocol":"desk-node-maintenance-v1","node":"deskb1x","boot_id":"old","expires":101}`, false}, {`invalid`, true}, {`{"protocol":"wrong"}`, true}} {
		os.WriteFile(p, []byte(v.body), 0600)
		if got := paused("deskb1x", "boot", 100, p); got != v.want {
			t.Fatalf("paused mismatch %s", v.body)
		}
	}
}
func TestBoundedMetadata(t *testing.T) {
	p := filepath.Join(t.TempDir(), "m")
	os.WriteFile(p, make([]byte, 16385), 0600)
	var v map[string]interface{}
	if read(p, &v) == nil {
		t.Fatal("accepted oversize")
	}
}
func TestMembership(t *testing.T) {
	var c Config
	json.Unmarshal([]byte(`{"protocol":"desk-vpn-ha-v1","node":"deskb1x","management":"192.168.1.101","vip":"192.168.1.254","interface":"wlan0","wireguard":"wgdesk0","port":51820,"nodes":{"deskb1x":"192.168.1.101","deskb2x":"192.168.1.102","deskb3x":"192.168.1.103"},"peers":{"key":"10.77.0.2/32"}}`), &c)
	if !valid(c) {
		t.Fatal("valid config rejected")
	}
	c.Management = "192.168.1.103"
	if valid(c) {
		t.Fatal("member mismatch accepted")
	}
	c.Management = "192.168.1.101"
	c.VIP = "192.168.1.250"
	if valid(c) {
		t.Fatal("wrong VIP accepted")
	}
}
