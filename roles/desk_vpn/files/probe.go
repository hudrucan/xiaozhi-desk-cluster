// Cheap bounded readiness and placement; no history writes or network API polling.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

type Config struct {
	Protocol, Node, Management, VIP, Interface, Wireguard string
	Port                                                  int
	Legacy                                                bool
	UIManaged                                             bool   `json:"ui_managed"`
	ServerPublic                                          string `json:"server_public"`
	Peers                                                 map[string]string
	Nodes                                                 map[string]string
}

func read(path string, target interface{}) error {
	f, e := os.Open(path)
	if e != nil {
		return e
	}
	defer f.Close()
	b, e := io.ReadAll(io.LimitReader(f, 16385))
	if e != nil || len(b) > 16384 {
		return errors.New("invalid bounded metadata")
	}
	return json.Unmarshal(b, target)
}
func valid(c Config) bool {
	return c.Protocol == "desk-vpn-ha-v1" && c.Interface == "wlan0" && c.Wireguard == "wgdesk0" && c.Port == 51820 && c.VIP == "192.168.1.254" && len(c.Nodes) == 3 && c.Nodes[c.Node] == c.Management && len(c.Peers) <= 32
}
func addresses(iface string) (map[string]bool, error) {
	i, e := net.InterfaceByName(iface)
	if e != nil {
		return nil, e
	}
	if i.Flags&net.FlagUp == 0 {
		return nil, errors.New("link down")
	}
	a, e := i.Addrs()
	if e != nil {
		return nil, e
	}
	out := map[string]bool{}
	for _, v := range a {
		ip, _, e := net.ParseCIDR(v.String())
		if e == nil {
			out[ip.String()] = true
		}
	}
	return out, nil
}
func paused(node, boot string, now int64, path string) bool {
	var v struct {
		Protocol, Node string
		Boot           string `json:"boot_id"`
		Expires        int64
	}
	e := read(path, &v)
	if os.IsNotExist(e) {
		return false
	}
	if e != nil {
		return true
	}
	return v.Protocol != "desk-node-maintenance-v1" || v.Node != node || v.Boot == "" || v.Expires <= 0 || v.Boot == boot && v.Expires > now
}
func command(name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 1800*time.Millisecond)
	defer cancel()
	v, e := exec.CommandContext(ctx, name, args...).Output()
	if len(v) > 8192 {
		return "", errors.New("oversize command result")
	}
	return strings.TrimSpace(string(v)), e
}
func syncReady(c Config, path string, now float64) bool {
	if !c.UIManaged {
		return true
	}
	var v struct {
		Protocol, Node string
		Observed       float64 `json:"observed_at"`
		Revision       int
		Applied        int `json:"applied_revision"`
		Ready          bool
	}
	if read(path, &v) != nil {
		return false
	}
	return v.Protocol == "desk-vpn-sync-v1" && v.Node == c.Node && v.Ready && v.Revision >= 0 && v.Applied == v.Revision && now-v.Observed >= -1 && now-v.Observed < 35
}
func ready(c Config) bool {
	if !syncReady(c, "/run/desk-vpn-ui-sync/status.json", float64(time.Now().Unix())) {
		return false
	}
	a, e := addresses(c.Interface)
	if e != nil || !a[c.Management] {
		return false
	}
	ownsVIP := a[c.VIP]
	a, e = addresses(c.Wireguard)
	if e != nil || !a["10.77.0.1"] {
		return false
	}
	boot, e := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if e != nil || paused(c.Node, strings.TrimSpace(string(boot)), time.Now().Unix(), "/run/desk-node-maintenance/state.json") {
		return false
	}
	v, e := os.ReadFile("/proc/sys/net/ipv4/ip_forward")
	if e != nil || strings.TrimSpace(string(v)) != "1" {
		return false
	}
	for field, wanted := range map[string]string{"listen-port": fmt.Sprint(c.Port), "public-key": c.ServerPublic, "fwmark": "0xca8d"} {
		s, e := command("/usr/bin/wg", "show", c.Wireguard, field)
		if e != nil || s != wanted {
			return false
		}
	}
	s, e := command("/usr/bin/wg", "show", c.Wireguard, "allowed-ips")
	if e != nil {
		return false
	}
	seen := map[string]string{}
	for _, line := range strings.Split(s, "\n") {
		p := strings.Fields(line)
		if len(p) == 2 {
			seen[p[0]] = p[1]
		}
	}
	if len(seen) != len(c.Peers) {
		return false
	}
	for k, v := range c.Peers {
		if seen[k] != v {
			return false
		}
	}
	for _, rule := range [][]string{{"filter", "INPUT", "-p", "udp", "--dport", "51820", "-j", "DESK_WG_PUBLIC"}, {"filter", "DESK_WG_PUBLIC", "-j", "DROP"}, {"filter", "FORWARD", "-i", "wgdesk0", "-j", "DESK_WG_FORWARD"}, {"filter", "DESK_WG_FORWARD", "-j", "DROP"}, {"nat", "INPUT", "-i", "wgdesk0", "-s", "10.77.0.0/24", "-d", "192.168.1.0/24", "-j", "DESK_WG_LOCAL"}} {
		_, e := command("/usr/sbin/iptables", append([]string{"-w", "1", "-t", rule[0], "-C"}, rule[1:]...)...)
		if e != nil {
			return false
		}
	}
	if ownsVIP {
		route, err := command("/usr/sbin/ip", "-4", "route", "get", "1.1.1.1", "mark", "51853")
		if err != nil || !strings.Contains(route, "src "+c.VIP) || !strings.Contains(route, "table 51853") {
			return false
		}
	}
	return true
}
func prefer(c Config, mode string) bool {
	path := "/etc/xiaozhi-ingress.json"
	if mode == "dns" {
		path = "/etc/desk-dns/vip.json"
	}
	var v struct{ VIP string }
	if read(path, &v) != nil || net.ParseIP(v.VIP) == nil {
		return false
	}
	a, e := addresses(c.Interface)
	return e == nil && !a[v.VIP]
}
func publishStatus(c Config, ready bool) {
	// Bounded atomic RAM snapshot for the existing dashboard; no peer keys or history.
	boot, e := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if e != nil {
		return
	}
	value := map[string]interface{}{"protocol": "desk-vpn-status-v1", "node": c.Node, "boot_id": strings.TrimSpace(string(boot)), "observed_at": time.Now().UTC(), "ready": ready}
	raw, e := json.Marshal(value)
	if e != nil {
		return
	}
	f, e := os.CreateTemp("/run/desk-vpn-vip", ".status-*")
	if e != nil {
		return
	}
	defer os.Remove(f.Name())
	if _, e = f.Write(raw); e != nil {
		f.Close()
		return
	}
	if e = f.Close(); e != nil {
		return
	}
	os.Rename(f.Name(), filepath.Join("/run/desk-vpn-vip", "status.json"))
}
func main() {
	var c Config
	if len(os.Args) != 2 || read("/etc/desk-vpn/public.json", &c) != nil || !valid(c) {
		os.Exit(1)
	}
	ok := false
	switch os.Args[1] {
	case "ready":
		ok = ready(c)
		publishStatus(c, ok)
	case "xiaozhi", "dns":
		ok = prefer(c, os.Args[1])
	default:
		os.Exit(1)
	}
	if !ok {
		os.Exit(1)
	}
}
