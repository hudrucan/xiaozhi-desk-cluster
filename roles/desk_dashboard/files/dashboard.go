// Read-only cluster overview. Standard library only; no database or packet path.
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const protocol = "desk-cluster-status-v1"
const interval = 5 * time.Second
const staleAfter = 20 * time.Second

type Member struct {
	ID         string `json:"id"`
	Private    string `json:"private"`
	Management string `json:"management"`
}
type Config struct {
	Node                string   `json:"node"`
	Port                int      `json:"port"`
	Members             []Member `json:"members"`
	ClientURL           string   `json:"client_url,omitempty"`
	MonitoringPlacement bool     `json:"monitoring_placement,omitempty"`
}
type Firmware struct {
	URL       string    `json:"url"`
	State     string    `json:"state"`
	At        time.Time `json:"observed_at"`
	Connected *bool     `json:"server_connected,omitempty"`
}
type SyncStatus struct {
	State          string `json:"state"`
	Revision       *int   `json:"revision"`
	WorkerRevision *int   `json:"worker_revision,omitempty"`
	CoreRevision   *int   `json:"core_revision,omitempty"`
}
type NetworkRevision struct {
	Term     int    `json:"term"`
	Node     string `json:"node"`
	Sequence int    `json:"sequence"`
}
type NetworkStatus struct {
	State        string           `json:"state"`
	Sync         string           `json:"sync"`
	Revision     *NetworkRevision `json:"revision,omitempty"`
	At           time.Time        `json:"observed_at"`
	Subnet       string           `json:"subnet"`
	ScanMinutes  int              `json:"scan_minutes"`
	ScanSchedule string           `json:"scan_schedule,omitempty"`
	VoterReady   bool             `json:"voter_ready"`
}

func validNetwork(n *NetworkStatus) bool {
	if n == nil {
		return true
	}
	if n.State != "active" && n.State != "starting" && n.State != "standby" {
		return false
	}
	if n.Sync != "synced" && n.Sync != "pending" {
		return false
	}
	if n.At.IsZero() || n.Subnet != "192.168.1.0/24" || n.ScanMinutes < 0 || n.ScanMinutes > 60 || len(n.ScanSchedule) > 128 || strings.ContainsAny(n.ScanSchedule, "\r\n\x00") {
		return false
	}
	r := n.Revision
	return r == nil || (r.Term >= 0 && r.Term <= 1000000000 && r.Sequence >= 0 && r.Sequence <= 1000000000 && (r.Node == "deskb1x" || r.Node == "deskb2x" || r.Node == "deskb3x"))
}

// Only bounded public metadata is read; stale Network state does not hide node telemetry.
func networkMetadata(meta map[string]any, node string, now time.Time) *NetworkStatus {
	stamp, ok := meta["observed_at"].(float64)
	if !ok || math.IsNaN(stamp) || math.IsInf(stamp, 0) || meta["protocol"] != "desk-network-v1" || meta["node"] != node {
		return nil
	}
	age := float64(now.UnixNano())/1e9 - stamp
	if age < -5 || age >= 15 {
		return nil
	}
	ready, ok := meta["voter_ready"].(bool)
	if !ok {
		return nil
	}
	state, _ := meta["state"].(string)
	sync, _ := meta["sync"].(string)
	subnet, _ := meta["subnet"].(string)
	scan := number(meta["scan_minutes"])
	schedule, _ := meta["scan_schedule"].(string)
	if scan == nil {
		return nil
	}
	result := &NetworkStatus{State: state, Sync: sync, At: time.Unix(0, int64(stamp*1e9)).UTC(), Subnet: subnet, ScanMinutes: *scan, ScanSchedule: schedule, VoterReady: ready}
	if meta["revision"] != nil {
		r, ok := meta["revision"].([]any)
		if !ok || len(r) != 3 {
			return nil
		}
		term, seq := number(r[0]), number(r[2])
		owner, ok := r[1].(string)
		if term == nil || seq == nil || !ok {
			return nil
		}
		result.Revision = &NetworkRevision{Term: *term, Node: owner, Sequence: *seq}
	}
	if !validNetwork(result) {
		return nil
	}
	return result
}

type HomeAssistantStatus struct {
	State        string    `json:"state"`
	Sync         string    `json:"sync"`
	Ready        bool      `json:"ready"`
	Revision     int       `json:"checkpoint_revision"`
	CheckpointAt time.Time `json:"checkpoint_at"`
	Pending      *bool     `json:"pending_disk_changes"`
	At           time.Time `json:"observed_at"`
}

func validHomeAssistant(h *HomeAssistantStatus) bool {
	if h == nil {
		return true
	}
	if h.State != "active" && h.State != "starting" && h.State != "checkpointing" && h.State != "standby" {
		return false
	}
	return (h.Sync == "synced" || h.Sync == "pending") && h.Revision >= 0 && h.Revision <= 1000000000 && !h.At.IsZero() && !h.CheckpointAt.IsZero() && (!h.Ready || h.State == "active")
}
func homeAssistantMetadata(meta map[string]any, node string, now time.Time) *HomeAssistantStatus {
	stamp, ok := meta["observed_at"].(float64)
	if !ok || math.IsNaN(stamp) || math.IsInf(stamp, 0) || meta["protocol"] != "desk-homeassistant-v1" || meta["node"] != node {
		return nil
	}
	age := float64(now.UnixNano())/1e9 - stamp
	if age < -5 || age >= 15 {
		return nil
	}
	revision := number(meta["checkpoint_revision"])
	checkpoint, ok := meta["checkpoint_at"].(float64)
	if !ok || math.IsNaN(checkpoint) || math.IsInf(checkpoint, 0) || checkpoint <= 0 || checkpoint > stamp+5 || revision == nil {
		return nil
	}
	state, _ := meta["state"].(string)
	syncState, _ := meta["sync"].(string)
	ready, ok := meta["ready"].(bool)
	if !ok {
		return nil
	}
	var pending *bool
	if v, ok := meta["pending_disk_changes"].(bool); ok {
		pending = &v
	}
	h := &HomeAssistantStatus{State: state, Sync: syncState, Ready: ready, Revision: *revision, CheckpointAt: time.Unix(0, int64(checkpoint*1e9)).UTC(), Pending: pending, At: time.Unix(0, int64(stamp*1e9)).UTC()}
	if !validHomeAssistant(h) {
		return nil
	}
	return h
}

type Node struct {
	HomeAssistant        *HomeAssistantStatus  `json:"homeassistant,omitempty"`
	Protocol             string                `json:"protocol"`
	ID                   string                `json:"id"`
	IP                   string                `json:"ip"`
	Boot                 string                `json:"boot_id"`
	At                   time.Time             `json:"observed_at"`
	CollectorStarted     *time.Time            `json:"collector_started_at,omitempty"`
	Online               bool                  `json:"online"`
	VIP                  map[string]*bool      `json:"vip"`
	VPNReady             *bool                 `json:"vpn_ready"`
	MQTT                 *int                  `json:"mqtt"`
	Core                 *int                  `json:"core"`
	Sessions             map[string]int        `json:"sessions"`
	Activity             map[string]*int       `json:"activity"`
	CPU                  *float64              `json:"cpu"`
	Memory               *float64              `json:"memory"`
	Temp                 *float64              `json:"temperature"`
	Uptime               *float64              `json:"uptime_seconds"`
	DNSHealth            DNSHealth             `json:"dns_health"`
	MaintenanceSupported bool                  `json:"maintenance_supported"`
	Admission            map[string]*bool      `json:"admission"`
	Connections          map[string]*int       `json:"connections"`
	DNSRate              *float64              `json:"dns_queries_per_second"`
	Protection           *bool                 `json:"dns_protection"`
	Components           map[string]string     `json:"components"`
	Config               map[string]SyncStatus `json:"configuration"`
	Events               []Event               `json:"events,omitempty"`
	ServiceRoles         map[string]string     `json:"service_roles,omitempty"`
	MonitoringPreferred  string                `json:"monitoring_preferred,omitempty"`
	MonitoringReady      bool                  `json:"monitoring_ready,omitempty"`
	Network              *NetworkStatus        `json:"network,omitempty"`
}
type DNSProbe struct {
	State   string    `json:"state"`
	Latency *float64  `json:"probe_latency_ms,omitempty"`
	At      time.Time `json:"observed_at"`
}
type DNSHealth struct {
	At       time.Time `json:"observed_at"`
	Mode     string    `json:"mode"`
	Backends []string  `json:"eligible_backends"`
	Resolver string    `json:"fallback_resolver,omitempty"`
	Local    DNSProbe  `json:"local"`
	IPv4     DNSProbe  `json:"ipv4"`
	IPv6     DNSProbe  `json:"ipv6"`
}

func dnsSkip(data []byte, offset int) (int, error) {
	for labels := 0; labels < 128; labels++ {
		if offset >= len(data) {
			return 0, errors.New("short DNS name")
		}
		size := int(data[offset])
		offset++
		if size == 0 {
			return offset, nil
		}
		if size&0xc0 == 0xc0 {
			if offset >= len(data) {
				break
			}
			return offset + 1, nil
		}
		if size > 63 || offset+size > len(data) {
			break
		}
		offset += size
	}
	return 0, errors.New("invalid DNS name")
}
func dnsMetadata(data, query []byte) (int, error) {
	if len(data) < 12 || len(data) > 4096 || !bytes.Equal(data[:2], query[:2]) {
		return 0, errors.New("invalid DNS reply")
	}
	flags := binary.BigEndian.Uint16(data[2:4])
	if flags&0x8000 == 0 || flags&0x7a0f != 0 || binary.BigEndian.Uint16(data[4:6]) != 1 {
		return 0, errors.New("DNS not successful")
	}
	pos, e := dnsSkip(data, 12)
	if e != nil || pos+4 > len(data) || !bytes.Equal(data[12:pos+4], query[12:]) {
		return 0, errors.New("DNS question mismatch")
	}
	pos += 4
	count := int(binary.BigEndian.Uint16(data[6:8]))
	if count > 16 {
		return 0, errors.New("oversize DNS answers")
	}
	for i := 0; i < count; i++ {
		pos, e = dnsSkip(data, pos)
		if e != nil || pos+10 > len(data) {
			break
		}
		kind, cls, length := binary.BigEndian.Uint16(data[pos:pos+2]), binary.BigEndian.Uint16(data[pos+2:pos+4]), int(binary.BigEndian.Uint16(data[pos+8:pos+10]))
		pos += 10
		if pos+length > len(data) {
			break
		}
		if kind == 16 && cls == 1 && length > 0 && int(data[pos]) == length-1 {
			text := string(data[pos+1 : pos+length])
			if strings.HasPrefix(text, "desk-dns-v1:") {
				mask, err := strconv.Atoi(strings.TrimPrefix(text, "desk-dns-v1:"))
				if err == nil && mask >= 0 && mask <= 15 {
					return mask, nil
				}
			}
		}
		pos += length
	}
	return 0, errors.New("DNS metadata missing")
}
func probeDNS(ctx context.Context, address, network string) (DNSProbe, int) {
	result := DNSProbe{State: "unavailable", At: time.Now().UTC()}
	query := make([]byte, 12)
	if _, e := rand.Read(query[:2]); e != nil {
		return result, 0
	}
	binary.BigEndian.PutUint16(query[2:4], 0x100)
	binary.BigEndian.PutUint16(query[4:6], 1)
	for _, label := range strings.Split("status.desk-dns.test", ".") {
		query = append(query, byte(len(label)))
		query = append(query, []byte(label)...)
	}
	query = append(query, 0, 0, 16, 0, 1)
	started := time.Now()
	dialer := net.Dialer{Timeout: 800 * time.Millisecond}
	conn, e := dialer.DialContext(ctx, network, address)
	if e != nil {
		return result, 0
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(800 * time.Millisecond))
	if _, e = conn.Write(query); e != nil {
		return result, 0
	}
	data := make([]byte, 4096)
	n, e := conn.Read(data)
	if e != nil {
		return result, 0
	}
	mask, e := dnsMetadata(data[:n], query)
	if e != nil {
		return result, 0
	}
	ms := float64(time.Since(started).Microseconds()) / 1000
	result.State = "ready"
	result.Latency = &ms
	return result, mask
}
func (d *Dashboard) collectDNS(ctx context.Context, node Node) DNSHealth {
	if time.Since(d.dnsHealth.At) < 15*time.Second {
		return d.dnsHealth
	}
	value := DNSHealth{At: time.Now().UTC(), Mode: "unknown", Backends: []string{}, IPv4: DNSProbe{State: "standby"}, IPv6: DNSProbe{State: "standby"}}
	value.Local, _ = probeDNS(ctx, net.JoinHostPort(d.member.Management, "5353"), "udp4")
	config := readJSON("/etc/desk-dns/vip.json", 65536)
	vip, _ := config["vip"].(string)
	if ip := net.ParseIP(vip); ip != nil && ip.To4() != nil && ip.IsPrivate() {
		var mask int
		value.IPv4, mask = probeDNS(ctx, net.JoinHostPort(vip, "53"), "udp4")
		if value.IPv4.State == "ready" {
			value.Mode = "unavailable"
			for i, m := range d.config.Members {
				if mask&(1<<i) != 0 {
					value.Backends = append(value.Backends, m.ID)
				}
			}
			if len(value.Backends) > 0 {
				value.Mode = "adguard"
			} else if mask&8 != 0 {
				value.Mode = "fallback"
				value.Resolver = "1.1.1.1"
			}
		}
		if interfaces, e := net.Interfaces(); e == nil {
			for _, iface := range interfaces {
				addresses, _ := iface.Addrs()
				for _, a := range addresses {
					ip, _, _ := net.ParseCIDR(a.String())
					if ip != nil && ip.Equal(net.ParseIP(d.member.Management)) {
						value.IPv6, _ = probeDNS(ctx, net.JoinHostPort("fe80::250%"+iface.Name, "53"), "udp6")
					}
				}
			}
		}
	}
	d.dnsHealth = value
	return value
}

type Event struct {
	ID      string    `json:"id"`
	At      time.Time `json:"at"`
	Node    string    `json:"node"`
	Kind    string    `json:"kind"`
	Message string    `json:"message"`
	Subject string    `json:"subject,omitempty"`
	From    string    `json:"from,omitempty"`
	To      string    `json:"to,omitempty"`
}

func notificationEvents(meta map[string]any, node string, now time.Time) []Event {
	var value struct {
		ObservedAt int64 `json:"observed_at"`
		Events     []struct {
			ID      string `json:"id"`
			Time    int64  `json:"time"`
			Message string `json:"message"`
		} `json:"events"`
	}
	raw, err := json.Marshal(meta)
	if err != nil || len(raw) > 16384 || json.Unmarshal(raw, &value) != nil || !panelFresh(time.Unix(value.ObservedAt, 0), now, 20*time.Second) || len(value.Events) > 16 {
		return nil
	}
	result := []Event{}
	for _, event := range value.Events {
		if len(event.ID) != 12 || strings.IndexFunc(event.ID, func(c rune) bool { return !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9') }) >= 0 || len(event.Message) > 256 || !panelFresh(time.Unix(event.Time, 0), now, 6*time.Hour) {
			continue
		}
		result = append(result, Event{ID: "ntfy-" + event.ID, At: time.Unix(event.Time, 0).UTC(), Node: node, Kind: "notification", Message: event.Message})
	}
	return result
}

type Snapshot struct {
	Protocol     string    `json:"protocol"`
	At           time.Time `json:"observed_at"`
	Nodes        []Node    `json:"nodes"`
	Events       []Event   `json:"events"`
	Observer     string    `json:"observer"`
	HistorySince time.Time `json:"history_since"`
	Firmware     Firmware  `json:"firmware"`
}
type cpuSample struct{ total, idle uint64 }
type Dashboard struct {
	config           Config
	member           Member
	client           *http.Client
	mu               sync.RWMutex
	local            Node
	snapshot         Snapshot
	cpu              cpuSample
	dnsHealth        DNSHealth
	dnsInstance      string
	dnsCount         int64
	dnsAt            float64
	slowAt           time.Time
	slowConfig       map[string]SyncStatus
	events           []Event
	sequence         int
	started          time.Time
	transitions      map[string]eventTransition
	boots            map[string]string
	collectors       map[string]time.Time
	telemetry        map[string]eventTransition
	firmware         Firmware
	monitorHealthy   bool
	preferredMonitor string
	preferredSince   time.Time
	monitorCandidate string
	candidateSince   time.Time
}

func unknown(member Member) Node {
	return Node{Protocol: protocol, ID: member.ID, IP: member.Management, VIP: map[string]*bool{"xiaozhi": nil, "dns": nil, "vpn": nil}, Activity: map[string]*int{"asr": nil, "llm": nil, "tts": nil}, Components: map[string]string{}, Config: map[string]SyncStatus{}, Sessions: map[string]int{}}
}
func number(v any) *int {
	n, ok := v.(float64)
	if !ok || math.IsNaN(n) || math.IsInf(n, 0) || n < 0 || n > 1e9 || n != math.Trunc(n) {
		return nil
	}
	p := int(n)
	return &p
}
func boolean(v any) *bool {
	b, ok := v.(bool)
	if !ok {
		return nil
	}
	return &b
}
func object(v any) map[string]any { m, _ := v.(map[string]any); return m }
func listHas(v any, id string) bool {
	a, _ := v.([]any)
	for _, x := range a {
		if x == id {
			return true
		}
	}
	return false
}
func readJSON(path string, limit int64) map[string]any {
	f, e := os.Open(path)
	if e != nil {
		return nil
	}
	defer f.Close()
	raw, e := io.ReadAll(io.LimitReader(f, limit+1))
	if e != nil || int64(len(raw)) > limit {
		return nil
	}
	var value map[string]any
	if json.Unmarshal(raw, &value) != nil {
		return nil
	}
	return value
}
func (d *Dashboard) get(ctx context.Context, url string) map[string]any {
	req, e := http.NewRequestWithContext(ctx, "GET", url, nil)
	if e != nil {
		return nil
	}
	res, e := d.client.Do(req)
	if e != nil {
		return nil
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		return nil
	}
	raw, e := io.ReadAll(io.LimitReader(res.Body, 65537))
	if e != nil || len(raw) > 65536 {
		return nil
	}
	var value map[string]any
	if json.Unmarshal(raw, &value) != nil {
		return nil
	}
	return value
}
func parallelGets(ctx context.Context, d *Dashboard, urls []string) []map[string]any {
	result := make([]map[string]any, len(urls))
	var wg sync.WaitGroup
	for i, url := range urls {
		wg.Add(1)
		go func(i int, url string) { defer wg.Done(); result[i] = d.get(ctx, url) }(i, url)
	}
	wg.Wait()
	return result
}
func vipOwner(path string, addresses map[string]bool) *bool {
	v := readJSON(path, 65536)
	ip, _ := v["vip"].(string)
	if net.ParseIP(ip) == nil || addresses == nil {
		return nil
	}
	owned := addresses[ip]
	return &owned
}
func vpnReadiness(v map[string]any, boot, node string, now time.Time) *bool {
	if boot == "" || v["protocol"] != "desk-vpn-status-v1" || v["boot_id"] != boot || v["node"] != node {
		return nil
	}
	stamp, ok := v["observed_at"].(string)
	at, e := time.Parse(time.RFC3339Nano, stamp)
	if !ok || e != nil || !panelFresh(at, now, 20*time.Second) {
		return nil
	}
	return boolean(v["ready"])
}
func localAddresses() map[string]bool {
	addresses, e := net.InterfaceAddrs()
	if e != nil {
		return nil
	}
	result := map[string]bool{}
	for _, a := range addresses {
		ip, _, e := net.ParseCIDR(a.String())
		if e == nil {
			result[ip.String()] = true
		}
	}
	return result
}
func uptime() float64 {
	raw, _ := os.ReadFile("/proc/uptime")
	f := strings.Fields(string(raw))
	if len(f) == 0 {
		return -1
	}
	v, e := strconv.ParseFloat(f[0], 64)
	if e != nil {
		return -1
	}
	return v
}
func parseCPU(raw string) cpuSample {
	var s cpuSample
	lines := strings.Split(raw, "\n")
	if len(lines) == 0 {
		return s
	}
	f := strings.Fields(lines[0])
	if len(f) < 9 || f[0] != "cpu" {
		return s
	}
	for i := 1; i <= 8; i++ {
		v, e := strconv.ParseUint(f[i], 10, 64)
		if e != nil {
			return cpuSample{}
		}
		s.total += v
		if i == 4 || i == 5 {
			s.idle += v
		}
	}
	return s
}
func cpuPercent(previous, current cpuSample) *float64 {
	if previous.total == 0 || current.total <= previous.total || current.idle < previous.idle {
		return nil
	}
	total := current.total - previous.total
	idle := current.idle - previous.idle
	if idle > total {
		return nil
	}
	v := math.Round((1-float64(idle)/float64(total))*1000) / 10
	return &v
}
func memoryPercent(raw string) *float64 {
	values := map[string]float64{}
	for _, line := range strings.Split(raw, "\n") {
		f := strings.Fields(line)
		if len(f) > 1 {
			v, e := strconv.ParseFloat(f[1], 64)
			if e == nil {
				values[strings.TrimSuffix(f[0], ":")] = v
			}
		}
	}
	total, avail := values["MemTotal"], values["MemAvailable"]
	if total <= 0 || avail <= 0 || avail > total {
		return nil
	}
	v := math.Round((1-avail/total)*1000) / 10
	return &v
}
func temperature() *float64 {
	paths, _ := filepath.Glob("/sys/class/thermal/thermal_zone*/temp")
	for _, p := range paths {
		raw, e := os.ReadFile(p)
		if e != nil {
			continue
		}
		v, e := strconv.ParseFloat(strings.TrimSpace(string(raw)), 64)
		if e == nil && v > 0 && v < 150000 {
			v = math.Round(v/100) / 10
			return &v
		}
	}
	return nil
}
func freshActivity(v map[string]any, boot, node, p string, maxAge, now float64) bool {
	stamp, ok := v["monotonic"].(float64)
	instance, _ := v["instance"].(string)
	idKey := "worker_id"
	if p == "desk-dns-activity-v1" {
		idKey = "node_id"
	}
	return ok && boot != "" && v["boot_id"] == boot && v[idKey] == node && v["protocol"] == p && len(instance) == 32 && now+0.1 >= stamp && now-stamp <= maxAge
}
func desiredSync(cp map[string]any, node string) SyncStatus {
	state := SyncStatus{State: "unknown"}
	if cp["protocol"] != "xiaozhi-control-plane-v1" || cp["node_id"] != node {
		return state
	}
	source, reconciliation := object(cp["configuration"]), object(cp["reconciliation"])
	state.Revision, state.State = number(source["desired_revision"]), "pending"
	ready := reconciliation["state"] == "ready"
	// A refresh retains the last validated snapshot. Applied runtime revisions
	// are still checked separately; new desired revisions remain pending.
	if reconciliation["state"] == "refreshing" && reconciliation["error_code"] == nil {
		last, ok := reconciliation["last_success_at"].(string)
		at, err := time.Parse(time.RFC3339Nano, last)
		ready = ok && err == nil && time.Since(at) >= 0 && time.Since(at) < 5*time.Minute
	}
	if ready && source["sync_status"] == "synced" && reconciliation["error_code"] == nil {
		state.State = "synced"
	} else if reconciliation["error_code"] != nil {
		state.State = "failed"
	}
	return state
}
func (d *Dashboard) slow(ctx context.Context) {
	if time.Since(d.slowAt) < 30*time.Second {
		return
	}
	d.slowAt = time.Now()
	ip := d.member.Private
	values := parallelGets(ctx, d, []string{"http://" + ip + ":8004/api/cluster", "http://127.0.0.1:3003/control/desk_policy", "http://" + ip + ":8092/api/desk-config/status", "http://" + ip + ":8004/api/settings/runtime", "http://" + ip + ":8091/api/health"})
	d.monitorHealthy = values[4]["code"] == float64(200)
	result := map[string]SyncStatus{}
	state := desiredSync(values[0], d.config.Node)
	result["xiaozhi"] = state
	if values[3]["protocol"] == "xiaozhi-runtime-apply-v1" {
		rows, _ := values[3]["nodes"].([]any)
		for _, item := range rows {
			row := object(item)
			if row["node_id"] == d.config.Node {
				s := result["xiaozhi"]
				s.WorkerRevision, s.CoreRevision = number(row["worker_revision"]), number(row["core_revision"])
				if s.State == "synced" && row["ready"] != true {
					s.State = "pending"
				}
				result["xiaozhi"] = s
			}
		}
	}
	dns := values[1]
	state = SyncStatus{State: "unknown"}
	if rev := number(dns["revision"]); rev != nil {
		state.Revision = rev
		state.State = "pending"
		if listHas(dns["ready"], d.config.Node) {
			state.State = "synced"
		}
		if dns["error"] != nil {
			state.State = "failed"
		}
	}
	result["dns"] = state
	monitor := values[2]
	state = SyncStatus{State: "unknown"}
	if rev := number(monitor["revision"]); rev != nil {
		state.Revision = rev
		state.State = "pending"
		applied := number(monitor["applied_revision"])
		if applied != nil && *applied == *rev && listHas(monitor["ready_nodes"], d.config.Node) {
			state.State = "synced"
		}
		if monitor["apply_error"] != nil {
			state.State = "failed"
		}
	}
	result["monitoring"] = state
	d.slowConfig = result
}
func (d *Dashboard) collect(ctx context.Context) {
	n := unknown(d.member)
	n.Online = true
	n.At = time.Now().UTC()
	n.CollectorStarted = &d.started
	boot, _ := os.ReadFile("/proc/sys/kernel/random/boot_id")
	n.Boot = strings.TrimSpace(string(boot))
	up := uptime()
	if up >= 0 {
		n.Uptime = &up
	}
	addresses := localAddresses()
	n.VIP["xiaozhi"] = vipOwner("/etc/xiaozhi-ingress.json", addresses)
	n.VIP["dns"] = vipOwner("/etc/desk-dns/vip.json", addresses)
	n.VIP["vpn"] = vipOwner("/etc/desk-vpn/public.json", addresses)
	n.VPNReady = vpnReadiness(readJSON("/run/desk-vpn-vip/status.json", 4096), n.Boot, n.ID, time.Now())
	n.Components["vpn"] = "unknown"
	if n.VPNReady != nil {
		n.Components["vpn"] = "unavailable"
		if *n.VPNReady {
			n.Components["vpn"] = "ready"
		}
	}
	raw, _ := os.ReadFile("/proc/stat")
	current := parseCPU(string(raw))
	n.CPU = cpuPercent(d.cpu, current)
	d.cpu = current
	raw, _ = os.ReadFile("/proc/meminfo")
	n.Memory = memoryPercent(string(raw))
	n.Temp = temperature()
	activity := readJSON("/run/xiaozhi-worker/activity.json", 65536)
	if freshActivity(activity, n.Boot, n.ID, "xiaozhi-worker-activity-v1", 3, uptime()) {
		for _, key := range []string{"asr", "llm", "tts"} {
			v := number(activity[key])
			if v != nil && *v <= 2 {
				n.Activity[key] = v
			}
		}
		n.Components["worker"] = "ready"
		n.Admission = map[string]*bool{"worker": boolean(activity["admission_paused"])}
		n.MaintenanceSupported = activity["maintenance_supported"] == true
	} else {
		n.Components["worker"] = "unavailable"
	}
	dns := readJSON("/run/desk-dns/activity.json", 65536)
	if freshActivity(dns, n.Boot, n.ID, "desk-dns-activity-v1", 3, uptime()) {
		count, ok := dns["queries"].(float64)
		stamp, _ := dns["monotonic"].(float64)
		instance, _ := dns["instance"].(string)
		if ok && count >= 0 && count < 1<<53 {
			if instance == d.dnsInstance && stamp > d.dnsAt && int64(count) >= d.dnsCount {
				rate := math.Round((count-float64(d.dnsCount))/(stamp-d.dnsAt)*10) / 10
				n.DNSRate = &rate
			}
			d.dnsCount, d.dnsInstance, d.dnsAt = int64(count), instance, stamp
		}
	} else {
		d.dnsInstance = ""
	}
	ip := d.member.Private
	urls := []string{"http://" + ip + ":8007/status", "http://" + ip + ":8000/status", "http://" + ip + ":8000/diagnostics", "http://127.0.0.1:3001/control/status", "http://127.0.0.1:2590/ready"}
	probeClient := d.config.ClientURL != "" && time.Since(d.firmware.At) >= 15*time.Second
	if probeClient {
		urls = append(urls, d.config.ClientURL+"api/status/system")
	}
	values := parallelGets(ctx, d, urls)
	gateway, core, voice, adguard := values[0], values[1], values[2], values[3]
	// Probe only the public readiness bit; the collector never reads auth state.
	n.Components["ntfy"] = "unavailable"
	if values[4]["ready"] == true {
		n.Components["ntfy"] = "ready"
	}
	if n.Admission == nil {
		n.Admission = map[string]*bool{}
	}
	n.Admission["core"], n.Admission["mqtt"] = boolean(core["admission_paused"]), boolean(gateway["admission_paused"])
	n.Connections = map[string]*int{"core": number(core["active_connections"]), "mqtt": number(gateway["open_mqtt_connections"])}
	dnsMaintenance := readJSON("/etc/desk-dns/maintenance.json", 2048)
	n.MaintenanceSupported = n.MaintenanceSupported && core["maintenance_supported"] == true && gateway["maintenance_supported"] == true && dnsMaintenance["protocol"] == "desk-dns-maintenance-v1" && dnsMaintenance["node"] == n.ID
	n.DNSHealth = d.collectDNS(ctx, n)
	n.Components["mqtt"] = "unavailable"
	if gateway["gateway_id"] == n.ID && (gateway["status"] == "ready" || (gateway["admission_paused"] == true && gateway["mqtt_listening"] == true && gateway["udp_listening"] == true)) {
		n.MQTT = number(gateway["active_mqtt_connections"])
		if n.MQTT != nil {
			n.Components["mqtt"] = "ready"
		}
	}
	n.Components["core"] = "unavailable"
	if core["protocol"] == "xiaozhi-core-transport-v1" && core["core_id"] == n.ID && core["status"] == "ready" {
		n.Core = number(core["active_sessions"])
		if n.Core != nil {
			n.Components["core"] = "ready"
		}
	}
	if voice["protocol"] == "xiaozhi-voice-diagnostics-v1" && voice["node_id"] == n.ID {
		for _, key := range []string{"idle", "asr", "asr_restarting", "llm", "tts", "done", "error", "aborted"} {
			if count := number(object(voice["sessions"])[key]); count != nil && *count <= 128 {
				n.Sessions[key] = *count
			}
		}
	}
	n.Components["dns"] = "unavailable"
	n.Protection = boolean(adguard["protection_enabled"])
	if n.Protection != nil {
		n.Components["dns"] = "ready"
	}
	d.slow(ctx)
	if probeClient {
		status := Firmware{URL: d.config.ClientURL, State: "offline", At: time.Now().UTC()}
		if status.URL == "" {
			status.State = "disabled"
		} else {
			value := values[5]
			parsed, _ := url.Parse(status.URL)
			version, ok := value["version"].(string)
			if ok && len(version) > 0 && len(version) <= 128 && value["ip"] == parsed.Hostname() && number(value["uptime_sec"]) != nil {
				status.State = "online"
				status.Connected = boolean(value["server_connected"])
			}
		}
		d.firmware = status
	}
	for key, s := range d.slowConfig {
		n.Config[key] = s
	}
	s := n.Config["xiaozhi"]
	if n.Components["worker"] != "ready" {
		s.WorkerRevision = nil
	}
	if n.Components["core"] == "ready" {
		s.CoreRevision = number(object(core["tts"])["revision"])
	}
	if s.State == "synced" && (s.Revision == nil || s.WorkerRevision == nil || s.CoreRevision == nil || *s.Revision != *s.WorkerRevision || *s.Revision != *s.CoreRevision) {
		s.State = "pending"
	}
	n.Config["xiaozhi"] = s
	n.ServiceRoles = map[string]string{}
	n.MonitoringReady = d.monitorHealthy
	n.HomeAssistant = homeAssistantMetadata(readJSON("/run/desk-homeassistant/public.json", 4096), d.config.Node, time.Now())
	n.Network = networkMetadata(readJSON("/run/desk-network/public.json", 2048), d.config.Node, time.Now())
	if n.Network != nil && n.Network.State == "active" {
		n.ServiceRoles["network"] = "active"
	}
	d.mu.Lock()
	n.Events = append([]Event{}, d.local.Events...)
	n.Events = mergeEvents(append(n.Events, notificationEvents(readJSON("/run/desk-ntfy/events.json", 16384), d.config.Node, time.Now())...), time.Now())
	d.local = n
	d.mu.Unlock()
}
func (d *Dashboard) event(n Node, kind, message string) {
	d.sequence++
	d.events = append(d.events, Event{ID: fmt.Sprintf("%s-%d-%d", d.config.Node, d.started.UnixNano(), d.sequence), At: time.Now().UTC(), Node: n.ID, Kind: kind, Message: message})
	if len(d.events) > 64 {
		d.events = d.events[len(d.events)-64:]
	}
}

type eventTransition struct {
	confirmed string
	candidate string
	since     time.Time
}

func (d *Dashboard) change(n Node, kind, subject, before, after, message string) {
	d.event(n, kind, message)
	i := len(d.events) - 1
	d.events[i].Subject = subject
	d.events[i].From = before
	d.events[i].To = after
}
func (d *Dashboard) transition(n Node, kind, subject, before, after, message string, delay time.Duration) {
	if d.transitions == nil {
		d.transitions = map[string]eventTransition{}
	}
	t, ok := d.transitions[subject]
	if !ok {
		if before == "" || before == "unknown" {
			d.transitions[subject] = eventTransition{confirmed: after}
			return
		}
		t.confirmed = before
	}
	if after == t.confirmed {
		t.candidate = ""
		d.transitions[subject] = t
		return
	}
	now := n.At
	if now.IsZero() {
		now = time.Now()
	}
	if t.candidate != after {
		t.candidate = after
		t.since = now
	}
	if now.Sub(t.since) >= delay {
		d.change(n, kind, subject, t.confirmed, after, message)
		t.confirmed = after
		t.candidate = ""
	}
	d.transitions[subject] = t
}
func (d *Dashboard) resetCandidate(subject string) {
	if t, ok := d.transitions[subject]; ok {
		t.candidate = ""
		d.transitions[subject] = t
	}
}
func configEventValue(s SyncStatus) string {
	if s.State == "" || s.State == "unknown" {
		return "unknown"
	}
	value := s.State
	if s.Revision != nil {
		value += fmt.Sprintf(" · revision %d", *s.Revision)
	}
	return value
}

// Only timeline emission is debounced. The snapshot remains unavailable on the
// first failed sample; stale ownership or activity must never look healthy.
const telemetryEventDelay = 30 * time.Second

func (d *Dashboard) observeTelemetry(previous, current Node, now time.Time) {
	if d.telemetry == nil {
		d.telemetry = map[string]eventTransition{}
	}
	t := d.telemetry[current.ID]
	if t.confirmed == "" {
		if previous.Online {
			t.confirmed = "Online"
		} else if current.Online {
			d.telemetry[current.ID] = eventTransition{confirmed: "Online"}
			return // Initial discovery is not recovery from a published outage.
		} else {
			return
		}
	}
	after := "Unavailable"
	if current.Online {
		after = "Online"
	}
	if after == t.confirmed {
		t.candidate = ""
	} else if current.Online {
		d.change(current, "node", "node", t.confirmed, after, "Node telemetry restored")
		t.confirmed, t.candidate = after, ""
	} else {
		if t.candidate != after {
			t.candidate, t.since = after, now
		}
		// Use observer time: unavailable nodes carry the last sample timestamp.
		if now.Sub(t.since) >= telemetryEventDelay {
			d.change(current, "node", "node", t.confirmed, after, "Node telemetry unavailable")
			t.confirmed, t.candidate = after, ""
		}
	}
	d.telemetry[current.ID] = t
}

func (d *Dashboard) observe(previous, current Node) {
	d.observeAt(previous, current, time.Now())
}
func (d *Dashboard) observeAt(previous, current Node, now time.Time) {
	// Retain only three safe boot identities in RAM so reboot detection survives
	// an intervening unavailable sample. Never publish the boot identities.
	if d.boots == nil {
		d.boots = map[string]string{}
	}
	bootChanged := false
	if current.Online && current.Boot != "" {
		old := d.boots[current.ID]
		if old == "" {
			old = previous.Boot
		}
		if old != "" && old != current.Boot {
			bootChanged = true
			d.change(current, "node", "node", "Previous boot", "New boot", "Node reboot detected")
		}
		d.boots[current.ID] = current.Boot
	}
	if current.Online && current.CollectorStarted != nil && !current.CollectorStarted.IsZero() {
		if d.collectors == nil {
			d.collectors = map[string]time.Time{}
		}
		old := d.collectors[current.ID]
		if old.IsZero() && previous.CollectorStarted != nil {
			old = *previous.CollectorStarted
		}
		if !old.IsZero() && !old.Equal(*current.CollectorStarted) && !bootChanged {
			d.change(current, "node", "telemetry_collector", "Previous collector", "New collector", "Telemetry collector restarted")
		}
		d.collectors[current.ID] = *current.CollectorStarted
	}
	d.observeTelemetry(previous, current, now)
	if previous.ID == "" {
		return
	}
	if !previous.Online || !current.Online {
		if current.ID == d.config.Node {
			for key := range d.transitions {
				d.resetCandidate(key)
			}
		}
		return
	}
	// Local state changes are emitted by their owning observer only.
	if current.ID != d.config.Node {
		return
	}
	mode := current.DNSHealth.Mode
	if mode == "adguard" || mode == "fallback" || mode == "unavailable" {
		d.transition(current, "service", "dns_routing", previous.DNSHealth.Mode, mode, "DNS routing "+mode, 0)
	} else {
		d.resetCandidate("dns_routing")
	}
	for _, key := range []string{"xiaozhi", "dns", "vpn"} {
		a, b := previous.VIP[key], current.VIP[key]
		if a != nil && b != nil && *a != *b {
			label := "Xiaozhi VIP"
			if key == "dns" {
				label = "DNS VIP"
			} else if key == "vpn" {
				label = "VPN VIP"
			}
			verb, from, to := "released", "Owner", "Standby"
			if *b {
				verb, from, to = "acquired", "Standby", "Owner"
			}
			d.change(current, "ownership", key, from, to, label+" "+verb)
		}
	}
	for _, key := range []string{"xiaozhi", "dns", "monitoring"} {
		a, b := previous.Config[key], current.Config[key]
		subject := "config_" + key
		if b.State == "" || b.State == "unknown" {
			d.resetCandidate(subject)
			continue
		}
		delay := time.Duration(0)
		// A new revision or explicit failure is useful immediately. Same-revision
		// pending must persist for 30 seconds; transient refreshes stay quiet.
		if b.State == "pending" && (a.Revision == nil || b.Revision == nil || *a.Revision == *b.Revision) {
			delay = 30 * time.Second
		}
		d.transition(current, "configuration", subject, configEventValue(a), configEventValue(b), key+" config "+configEventValue(b), delay)
	}
	for _, key := range []string{"mqtt", "core", "worker", "dns"} {
		a, b := previous.Components[key], current.Components[key]
		subject := "service_" + key
		if b != "ready" && b != "unavailable" {
			d.resetCandidate(subject)
			continue
		}
		delay := time.Duration(0)
		if b == "unavailable" {
			delay = 15 * time.Second
		}
		d.transition(current, "service", subject, a, b, key+" service "+b, delay)
	}
}

func validateNode(n Node, m Member, now time.Time) bool {
	if n.CollectorStarted != nil && (n.CollectorStarted.IsZero() || n.CollectorStarted.After(n.At.Add(5*time.Second))) {
		return false
	}
	if n.Protocol != protocol || n.ID != m.ID || n.IP != m.Management || !n.Online || n.At.IsZero() || now.Sub(n.At) > staleAfter || n.At.Sub(now) > 5*time.Second {
		return false
	}
	for _, v := range []*float64{n.CPU, n.Memory, n.Temp, n.DNSRate, n.Uptime} {
		if v != nil && (math.IsNaN(*v) || math.IsInf(*v, 0) || *v < 0) {
			return false
		}
	}
	if n.CPU != nil && *n.CPU > 100 || n.Memory != nil && *n.Memory > 100 {
		return false
	}
	for _, v := range []*int{n.MQTT, n.Core} {
		if v != nil && (*v < 0 || *v > 1024) {
			return false
		}
	}
	if n.DNSHealth.Mode != "" && n.DNSHealth.Mode != "unknown" && n.DNSHealth.Mode != "adguard" && n.DNSHealth.Mode != "fallback" && n.DNSHealth.Mode != "unavailable" {
		return false
	}
	if len(n.DNSHealth.Backends) > 3 {
		return false
	}
	seen := map[string]bool{}
	for _, id := range n.DNSHealth.Backends {
		if (id != "deskb1x" && id != "deskb2x" && id != "deskb3x") || seen[id] {
			return false
		}
		seen[id] = true
	}
	if n.DNSHealth.Resolver != "" && n.DNSHealth.Resolver != "1.1.1.1" {
		return false
	}
	for _, probe := range []DNSProbe{n.DNSHealth.Local, n.DNSHealth.IPv4, n.DNSHealth.IPv6} {
		if probe.State != "" && probe.State != "ready" && probe.State != "standby" && probe.State != "unavailable" {
			return false
		}
		if probe.Latency != nil && (math.IsNaN(*probe.Latency) || math.IsInf(*probe.Latency, 0) || *probe.Latency < 0 || *probe.Latency > 10000) {
			return false
		}
	}
	for key, v := range n.Connections {
		if (key != "core" && key != "mqtt") || v != nil && (*v < 0 || *v > 1024) {
			return false
		}
	}
	for key := range n.Admission {
		if key != "core" && key != "mqtt" && key != "worker" {
			return false
		}
	}
	for key, v := range n.Activity {
		if key != "asr" && key != "llm" && key != "tts" || v != nil && (*v < 0 || *v > 2) {
			return false
		}
	}
	for key, value := range n.Config {
		if key != "xiaozhi" && key != "dns" && key != "monitoring" {
			return false
		}
		if value.State != "synced" && value.State != "pending" && value.State != "failed" && value.State != "unknown" {
			return false
		}
	}
	for key, value := range n.Components {
		if key != "mqtt" && key != "core" && key != "worker" && key != "dns" && key != "vpn" && key != "ntfy" {
			return false
		}
		if value != "ready" && value != "unavailable" && !(key == "vpn" && value == "unknown") {
			return false
		}
	}
	if n.MonitoringPreferred != "" && n.MonitoringPreferred != "deskb1x" && n.MonitoringPreferred != "deskb2x" && n.MonitoringPreferred != "deskb3x" {
		return false
	}
	if !validHomeAssistant(n.HomeAssistant) || !validNetwork(n.Network) {
		return false
	}
	for key, value := range n.ServiceRoles {
		if key != "network" || value != "trial" && value != "active" {
			return false
		}
	}
	for key, count := range n.Sessions {
		switch key {
		case "idle", "asr", "asr_restarting", "llm", "tts", "done", "error", "aborted":
		default:
			return false
		}
		if count < 0 || count > 128 {
			return false
		}
	}
	if len(n.Events) > 64 {
		return false
	}
	for _, event := range n.Events {
		if len(event.ID) > 192 || len(event.Message) > 256 || len(event.Node) > 32 || event.At.IsZero() || len(event.Subject) > 32 || len(event.From) > 96 || len(event.To) > 96 {
			return false
		}
	}
	return true
}
func (d *Dashboard) aggregate(ctx context.Context) {
	d.mu.RLock()
	local, previous := d.local, d.snapshot
	d.mu.RUnlock()
	nodes := make([]Node, len(d.config.Members))
	var wg sync.WaitGroup
	for i, m := range d.config.Members {
		wg.Add(1)
		go func(i int, m Member) {
			defer wg.Done()
			n := unknown(m)
			if m.ID == d.config.Node {
				n = local
			} else {
				raw := d.get(ctx, "http://"+net.JoinHostPort(m.Private, strconv.Itoa(d.config.Port))+"/local")
				encoded, e := json.Marshal(raw)
				if e == nil {
					_ = json.Unmarshal(encoded, &n)
				}
			}
			if !validateNode(n, m, time.Now()) {
				n = unknown(m)
				for _, old := range previous.Nodes {
					if old.ID == m.ID {
						n.At = old.At
						break
					}
				}
			}
			nodes[i] = n
		}(i, m)
	}
	wg.Wait()
	d.mu.Lock()
	if d.config.MonitoringPlacement {
		d.selectMonitoring(nodes, time.Now())
	}
	if d.local.VIP["xiaozhi"] != nil && *d.local.VIP["xiaozhi"] {
		d.local.MonitoringPreferred = d.preferredMonitor
	} else {
		d.local.MonitoringPreferred = ""
	}
	for i := range nodes {
		if nodes[i].ID == d.config.Node {
			nodes[i].MonitoringPreferred = d.local.MonitoringPreferred
		}
	}
	d.mu.Unlock()
	old := map[string]Node{}
	for _, n := range previous.Nodes {
		old[n.ID] = n
	}
	for _, n := range nodes {
		d.observe(old[n.ID], n)
	}
	events := append(append([]Event{}, previous.Events...), d.events...)
	for i, node := range nodes {
		events = append(events, node.Events...)
		nodes[i].Events = nil
	}
	events = mergeEvents(events, time.Now())
	d.mu.Lock()
	d.local.Events = append([]Event{}, events...)
	d.snapshot = Snapshot{Protocol: protocol, At: time.Now().UTC(), Nodes: nodes, Events: events, Observer: d.config.Node, HistorySince: d.started, Firmware: d.firmware}
	d.mu.Unlock()
}
func mergeEvents(events []Event, now time.Time) []Event {
	sort.Slice(events, func(i, j int) bool {
		if events[i].At.Equal(events[j].At) {
			return events[i].ID > events[j].ID
		}
		return events[i].At.After(events[j].At)
	})
	seen := map[string]bool{}
	result := []Event{}
	for _, event := range events {
		if seen[event.ID] || now.Sub(event.At) > 6*time.Hour || event.At.Sub(now) > 5*time.Second {
			continue
		}
		seen[event.ID] = true
		duplicate := false
		if event.Kind == "node" {
			for _, kept := range result {
				if kept.Kind == event.Kind && kept.Node == event.Node && kept.Message == event.Message && kept.At.Sub(event.At) < 15*time.Second {
					duplicate = true
					break
				}
			}
		}
		if !duplicate {
			result = append(result, event)
		}
		if len(result) == 64 {
			break
		}
	}
	return result
}

// Panel is a bounded projection of cached telemetry for small touch displays.
// It never performs probes, exposes peer endpoints, or accepts control actions.
const panelProtocol = "desk-cluster-panel-v1"
const panelMaxBytes = 8192

type PanelOwner struct {
	State string  `json:"state"`
	Node  *string `json:"node"`
}
type PanelJobs struct {
	ASR    *int `json:"asr"`
	LLMVLM *int `json:"llm_vlm"`
	TTS    *int `json:"tts"`
}
type PanelSync struct {
	State    string `json:"state"`
	Revision *int   `json:"revision"`
}
type PanelNetwork struct {
	State        string           `json:"state"`
	Sync         string           `json:"sync"`
	Revision     *NetworkRevision `json:"revision"`
	AgeMS        *int64           `json:"age_ms"`
	Subnet       string           `json:"subnet"`
	ScanMinutes  int              `json:"scan_minutes"`
	ScanSchedule string           `json:"scan_schedule,omitempty"`
	VoterReady   bool             `json:"voter_ready"`
}
type PanelHomeAssistant struct {
	State           string `json:"state"`
	Sync            string `json:"sync"`
	Ready           bool   `json:"ready"`
	Revision        int    `json:"checkpoint_revision"`
	Pending         *bool  `json:"pending_disk_changes"`
	AgeMS           *int64 `json:"age_ms"`
	CheckpointAgeMS *int64 `json:"checkpoint_age_ms"`
}
type PanelNode struct {
	ID              string               `json:"id"`
	IP              string               `json:"ip"`
	Online          bool                 `json:"online"`
	AgeMS           *int64               `json:"age_ms"`
	CPU             *float64             `json:"cpu_percent"`
	Memory          *float64             `json:"memory_percent"`
	Temperature     *float64             `json:"temperature_c"`
	VIP             map[string]*bool     `json:"vip"`
	VPNReady        *bool                `json:"vpn_ready"`
	MonitoringReady *bool                `json:"monitoring_ready,omitempty"`
	Jobs            PanelJobs            `json:"jobs"`
	MQTT            *int                 `json:"mqtt_clients"`
	Core            *int                 `json:"conversations"`
	DNS             *bool                `json:"dns_eligible"`
	DNSRate         *float64             `json:"dns_queries_per_second"`
	Admission       map[string]*bool     `json:"admission_paused"`
	Components      map[string]string    `json:"components"`
	Config          map[string]PanelSync `json:"configuration"`
	Network         *PanelNetwork        `json:"network,omitempty"`
	HomeAssistant   *PanelHomeAssistant  `json:"homeassistant,omitempty"`
}
type PanelDNS struct {
	Mode     string   `json:"mode"`
	Eligible []string `json:"eligible"`
	Resolver *string  `json:"fallback_resolver"`
}
type PanelEvent struct {
	ID      string    `json:"id,omitempty"`
	At      time.Time `json:"at"`
	Node    string    `json:"node"`
	Kind    string    `json:"kind"`
	Message string    `json:"message"`
}
type PanelClient struct {
	State     string `json:"state"`
	Connected *bool  `json:"server_connected"`
}
type Panel struct {
	Protocol            string                `json:"protocol"`
	State               string                `json:"state"`
	At                  *time.Time            `json:"observed_at"`
	AgeMS               *int64                `json:"age_ms"`
	PollMS              int                   `json:"poll_after_ms"`
	StaleMS             int                   `json:"stale_after_ms"`
	Owners              map[string]PanelOwner `json:"owners"`
	DNS                 PanelDNS              `json:"dns"`
	Nodes               []PanelNode           `json:"nodes"`
	Client              PanelClient           `json:"client"`
	Events              []PanelEvent          `json:"events"`
	MonitoringPreferred *string               `json:"monitoring_preferred"`
}

func panelAge(at, now time.Time) *int64 {
	if at.IsZero() {
		return nil
	}
	age := now.Sub(at).Milliseconds()
	if age < 0 {
		age = 0
	}
	return &age
}
func panelFresh(at, now time.Time, maximum time.Duration) bool {
	return !at.IsZero() && now.Sub(at) <= maximum && at.Sub(now) <= 5*time.Second
}
func panelMetric(value *float64) *float64 {
	if value == nil {
		return nil
	}
	v := *value
	if math.Abs(v) <= math.MaxFloat64/10 {
		v = math.Round(v*10) / 10
	}
	return &v
}
func panelNetwork(value *NetworkStatus, now time.Time) *PanelNetwork {
	if value == nil || !validNetwork(value) || !panelFresh(value.At, now, 15*time.Second) || now.Sub(value.At) >= 15*time.Second {
		return nil
	}
	return &PanelNetwork{State: value.State, Sync: value.Sync, Revision: value.Revision,
		AgeMS: panelAge(value.At, now), Subnet: value.Subnet, ScanMinutes: value.ScanMinutes,
		ScanSchedule: value.ScanSchedule, VoterReady: value.VoterReady}
}
func panelHomeAssistant(h *HomeAssistantStatus, now time.Time) *PanelHomeAssistant {
	if h == nil || !validHomeAssistant(h) || !panelFresh(h.At, now, 15*time.Second) || now.Sub(h.At) >= 15*time.Second {
		return nil
	}
	return &PanelHomeAssistant{State: h.State, Sync: h.Sync, Ready: h.Ready, Revision: h.Revision, Pending: h.Pending, AgeMS: panelAge(h.At, now), CheckpointAgeMS: panelAge(h.CheckpointAt, now)}
}
func panelSnapshot(snapshot Snapshot, members []Member, now time.Time) Panel {
	p := Panel{Protocol: panelProtocol, State: "unavailable", AgeMS: panelAge(snapshot.At, now), PollMS: int(interval / time.Millisecond), StaleMS: int(staleAfter / time.Millisecond),
		Owners: map[string]PanelOwner{"xiaozhi": {State: "unknown"}, "dns": {State: "unknown"}, "vpn": {State: "unknown"}, "network": {State: "unknown"}, "homeassistant": {State: "unknown"}}, DNS: PanelDNS{Mode: "unknown", Eligible: []string{}}, Nodes: []PanelNode{}, Client: PanelClient{State: "unknown"}, Events: []PanelEvent{}}
	if !snapshot.At.IsZero() {
		at := snapshot.At
		p.At = &at
	}
	current := snapshot.Protocol == protocol && panelFresh(snapshot.At, now, staleAfter) && len(snapshot.Nodes) == len(members)
	fresh := map[string]Node{}
	if current {
		for _, m := range members {
			for _, n := range snapshot.Nodes {
				if validateNode(n, m, now) {
					fresh[m.ID] = n
					break
				}
			}
		}
	}
	if current {
		p.State = "degraded"
		if len(fresh) == len(members) {
			p.State = "ready"
		}
	}
	for _, key := range []string{"xiaozhi", "dns", "vpn"} {
		owners := []string{}
		for _, m := range members {
			if n, ok := fresh[m.ID]; ok && n.VIP[key] != nil && *n.VIP[key] {
				owners = append(owners, m.ID)
			}
		}
		if len(owners) == 1 {
			id := owners[0]
			p.Owners[key] = PanelOwner{State: "known", Node: &id}
		} else if len(owners) > 1 {
			p.Owners[key] = PanelOwner{State: "conflict"}
			p.State = "degraded"
		}
		if len(owners) == 0 && current && key != "vpn" {
			p.State = "degraded"
		}
	}
	for _, m := range members {
		if value, ok := fresh[m.ID]; ok {
			info := panelNetwork(value.Network, now)
			if info == nil || info.State != "active" || !info.VoterReady {
				continue
			}
			if p.Owners["network"].State == "unknown" {
				id := m.ID
				p.Owners["network"] = PanelOwner{State: "known", Node: &id}
			} else {
				p.Owners["network"] = PanelOwner{State: "conflict"}
			}
		}
	}
	for _, m := range members {
		if n, ok := fresh[m.ID]; ok {
			h := panelHomeAssistant(n.HomeAssistant, now)
			if h == nil || h.State == "standby" {
				continue
			}
			if p.Owners["homeassistant"].State == "unknown" {
				id := m.ID
				p.Owners["homeassistant"] = PanelOwner{State: "known", Node: &id}
			} else {
				p.Owners["homeassistant"] = PanelOwner{State: "conflict"}
				p.State = "degraded"
			}
		}
	}
	if owner := p.Owners["xiaozhi"]; owner.Node != nil {
		preferred := fresh[*owner.Node].MonitoringPreferred
		if target, ok := fresh[preferred]; ok && target.MonitoringReady {
			id := preferred
			p.MonitoringPreferred = &id
		}
	}
	if owner := p.Owners["dns"]; owner.Node != nil {
		health := fresh[*owner.Node].DNSHealth
		if panelFresh(health.At, now, 30*time.Second) {
			switch health.Mode {
			case "adguard":
				p.DNS.Mode = "adguard"
				for _, m := range members {
					for _, id := range health.Backends {
						if id == m.ID {
							if _, ok := fresh[id]; ok {
								p.DNS.Eligible = append(p.DNS.Eligible, id)
							}
							break
						}
					}
				}
			case "fallback":
				p.DNS.Mode = "fallback"
				if health.Resolver == "1.1.1.1" {
					value := health.Resolver
					p.DNS.Resolver = &value
				}
			case "unavailable":
				p.DNS.Mode = "unavailable"
			}
		}
	}
	if current && (p.DNS.Mode != "adguard" || len(p.DNS.Eligible) != len(members)) {
		p.State = "degraded"
	}
	for _, m := range members {
		n := PanelNode{ID: m.ID, IP: m.Management, VIP: map[string]*bool{"xiaozhi": nil, "dns": nil, "vpn": nil}, Admission: map[string]*bool{"worker": nil, "core": nil, "mqtt": nil}, Components: map[string]string{}, Config: map[string]PanelSync{}}
		for _, old := range snapshot.Nodes {
			if old.ID == m.ID {
				n.AgeMS = panelAge(old.At, now)
				break
			}
		}
		for _, key := range []string{"worker", "core", "mqtt", "dns"} {
			n.Components[key] = "unknown"
		}
		n.Components["ntfy"] = "unknown"
		for _, key := range []string{"xiaozhi", "dns", "monitoring"} {
			n.Config[key] = PanelSync{State: "unknown"}
		}
		if value, ok := fresh[m.ID]; ok {
			if s := value.Components["ntfy"]; s == "ready" || s == "unavailable" {
				n.Components["ntfy"] = s
			}
			n.Online = true
			n.CPU = panelMetric(value.CPU)
			n.Memory = panelMetric(value.Memory)
			n.Temperature = panelMetric(value.Temp)
			n.DNSRate = panelMetric(value.DNSRate)
			n.Jobs = PanelJobs{ASR: value.Activity["asr"], LLMVLM: value.Activity["llm"], TTS: value.Activity["tts"]}
			n.MQTT = value.MQTT
			n.Core = value.Core
			n.VPNReady = value.VPNReady
			monitorReady := value.MonitoringReady
			n.MonitoringReady = &monitorReady
			n.Network = panelNetwork(value.Network, now)
			n.HomeAssistant = panelHomeAssistant(value.HomeAssistant, now)
			n.Components["vpn"] = value.Components["vpn"]
			for _, key := range []string{"xiaozhi", "dns", "vpn"} {
				n.VIP[key] = value.VIP[key]
			}
			for _, key := range []string{"worker", "core", "mqtt"} {
				n.Admission[key] = value.Admission[key]
			}
			for _, key := range []string{"worker", "core", "mqtt", "dns"} {
				if s := value.Components[key]; s != "" {
					n.Components[key] = s
				}
				if n.Components[key] != "ready" && current {
					p.State = "degraded"
				}
			}
			for _, key := range []string{"xiaozhi", "dns", "monitoring"} {
				if s, ok := value.Config[key]; ok {
					n.Config[key] = PanelSync{State: s.State, Revision: s.Revision}
				}
			}
			if p.DNS.Mode != "unknown" {
				eligible := false
				for _, id := range p.DNS.Eligible {
					if id == m.ID {
						eligible = true
					}
				}
				n.DNS = &eligible
			}
		}
		p.Nodes = append(p.Nodes, n)
	}
	if current && panelFresh(snapshot.Firmware.At, now, 30*time.Second) {
		p.Client = PanelClient{State: snapshot.Firmware.State, Connected: snapshot.Firmware.Connected}
	}
	if current {
		for _, event := range snapshot.Events {
			if len(p.Events) == 12 {
				break
			}
			if !panelFresh(event.At, now, 6*time.Hour) || len(event.Message) > 256 || len(event.Node) > 32 {
				continue
			}
			switch event.Kind {
			case "ownership", "configuration", "node", "service", "notification":
			default:
				continue
			}
			p.Events = append(p.Events, PanelEvent{ID: event.ID, At: event.At, Node: event.Node, Kind: event.Kind, Message: event.Message})
			// Events share the existing byte budget with node telemetry.
			if raw, err := json.Marshal(p); err != nil || len(raw) > panelMaxBytes {
				p.Events = p.Events[:len(p.Events)-1]
				break
			}
		}
	}
	return p
}

func (d *Dashboard) allowed(req *http.Request) bool {
	host, _, e := net.SplitHostPort(req.RemoteAddr)
	if e != nil {
		return false
	}
	if host == "127.0.0.1" {
		return true
	}
	for _, m := range d.config.Members {
		if host == m.Private {
			return true
		}
	}
	return false
}
func (d *Dashboard) handle(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if !d.allowed(r) {
		http.Error(w, "Forbidden", 403)
		return
	}
	if r.Method != "GET" {
		w.Header().Set("Allow", "GET")
		http.Error(w, "Read-only API", 405)
		return
	}
	if r.URL.RawQuery != "" {
		http.Error(w, "Query not supported", 400)
		return
	}
	d.mu.RLock()
	defer d.mu.RUnlock()
	var value any
	switch r.URL.Path {
	case "/":
		// A normal backend response avoids HAProxy's small inline-return buffer.
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		http.ServeFile(w, r, "/etc/desk-ui/index.html")
		return
	case "/health":
		// Stay available when peers fail, but never admit a collector that rejects its own snapshot.
		ready := len(d.snapshot.Nodes) == len(d.config.Members) && time.Since(d.snapshot.At) < staleAfter && validateNode(d.local, d.member, time.Now())
		if !ready {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
		value = map[string]bool{"ready": ready}
	case "/local":
		value = d.local
	case "/api/cluster/status":
		value = d.snapshot
	case "/api/cluster/panel":
		panel := panelSnapshot(d.snapshot, d.config.Members, time.Now())
		raw, err := json.Marshal(panel)
		if err != nil || len(raw) > panelMaxBytes {
			http.Error(w, "Panel projection unavailable", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Retry-After", "5")
		w.Header().Set("Content-Length", strconv.Itoa(len(raw)))
		if panel.State == "unavailable" {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
		_, _ = w.Write(raw)
		return
	default:
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(value)
}
func loadConfig(path string) (Config, error) {
	var c Config
	raw, e := os.ReadFile(path)
	if e != nil {
		return c, e
	}
	if len(raw) > 16384 || json.Unmarshal(raw, &c) != nil || c.Port < 1024 || c.Port > 65535 || len(c.Members) != 3 {
		return c, errors.New("invalid configuration")
	}
	ids, ips := map[string]bool{}, map[string]bool{}
	found := false
	for _, m := range c.Members {
		if m.ID != "deskb1x" && m.ID != "deskb2x" && m.ID != "deskb3x" || ids[m.ID] || ips[m.Private] || net.ParseIP(m.Private) == nil || !net.ParseIP(m.Private).IsPrivate() || net.ParseIP(m.Management) == nil || !net.ParseIP(m.Management).IsPrivate() {
			return c, errors.New("invalid member")
		}
		ids[m.ID] = true
		ips[m.Private] = true
		if m.ID == c.Node {
			found = true
		}
	}
	if !found {
		return c, errors.New("invalid local member")
	}
	if c.ClientURL != "" {
		u, e := url.Parse(c.ClientURL)
		if e != nil || u.Scheme != "http" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Path != "/" || u.Port() != "8080" {
			return c, errors.New("invalid client URL")
		}
		ip := net.ParseIP(u.Hostname())
		if ip == nil || !ip.IsPrivate() || ip.To4() == nil {
			return c, errors.New("invalid client address")
		}
	}
	return c, nil
}
func main() {
	path := "/etc/desk-dashboard/config.json"
	if len(os.Args) == 3 && os.Args[1] == "--config" {
		path = os.Args[2]
	}
	config, e := loadConfig(path)
	if e != nil {
		fmt.Fprintln(os.Stderr, "Dashboard configuration unavailable")
		os.Exit(1)
	}
	d := &Dashboard{config: config, started: time.Now().UTC(), slowConfig: map[string]SyncStatus{}, client: &http.Client{Timeout: 1800 * time.Millisecond, Transport: &http.Transport{Proxy: nil, MaxIdleConns: 16, MaxIdleConnsPerHost: 2, IdleConnTimeout: 45 * time.Second}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}
	for _, m := range config.Members {
		if m.ID == config.Node {
			d.member = m
		}
	}
	d.local = unknown(d.member)
	d.snapshot = Snapshot{Protocol: protocol, Nodes: []Node{}, Events: []Event{}}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	// One bounded collector per node. Browser polls are cached and cannot multiply probes.
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			sample, cancelSample := context.WithTimeout(ctx, 4*time.Second)
			d.collect(sample)
			cancelSample()
			peers, cancelPeers := context.WithTimeout(ctx, 2*time.Second)
			d.aggregate(peers)
			cancelPeers()
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
	if config.MonitoringPlacement {
		go d.monitorAgent(ctx)
	}
	server := &http.Server{Addr: net.JoinHostPort(d.member.Private, strconv.Itoa(config.Port)), Handler: http.HandlerFunc(d.handle), ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 3 * time.Second, WriteTimeout: 3 * time.Second, IdleTimeout: 15 * time.Second, MaxHeaderBytes: 8192}
	go func() {
		<-ctx.Done()
		stop, finish := context.WithTimeout(context.Background(), 3*time.Second)
		defer finish()
		_ = server.Shutdown(stop)
	}()
	if e := server.ListenAndServe(); e != nil && e != http.ErrServerClosed {
		fmt.Fprintln(os.Stderr, "Dashboard listener unavailable")
		os.Exit(1)
	}
}

// Only initial session weights change; healthy sticky sessions remain on their hub.
func monitorScore(n Node, now time.Time) (float64, bool) {
	if !n.Online || n.At.IsZero() || now.Sub(n.At) > staleAfter || n.At.Sub(now) > 5*time.Second || !n.MonitoringReady || n.CPU == nil || math.IsNaN(*n.CPU) || *n.CPU < 0 || *n.CPU > 100 {
		return 0, false
	}
	for _, paused := range n.Admission {
		if paused != nil && *paused {
			return 0, false
		}
	}
	score := *n.CPU
	owners := 0
	for role, cost := range map[string]float64{"xiaozhi": 20, "dns": 10, "vpn": 5} {
		if n.VIP[role] != nil && *n.VIP[role] {
			score += cost
			owners++
		}
	}
	for _, job := range []string{"asr", "llm", "tts"} {
		if count := n.Activity[job]; count != nil && *count > 0 {
			score += 30
		}
	}
	if n.ServiceRoles["network"] != "" {
		score += 20
		owners++
	}
	if h := panelHomeAssistant(n.HomeAssistant, now); h != nil && h.State != "standby" {
		score += 20
		owners++
	}
	// Spread new monitoring sessions before stacking another role on busy owners.
	// Health/admission still decide eligibility; this changes only the preference.
	if owners > 1 {
		score += float64(owners-1) * 100
	}
	return score, true
}
func (d *Dashboard) selectMonitoring(nodes []Node, now time.Time) {
	best := ""
	lowest := math.MaxFloat64
	current := math.MaxFloat64
	for _, n := range nodes {
		score, ok := monitorScore(n, now)
		if !ok {
			continue
		}
		if n.ID == d.preferredMonitor {
			current = score
		}
		if score < lowest || score == lowest && n.ID < best {
			best = n.ID
			lowest = score
		}
	}
	if best == "" {
		d.preferredMonitor = ""
		d.monitorCandidate = ""
		return
	}
	if current == math.MaxFloat64 {
		d.preferredMonitor = best
		d.preferredSince = now
		d.monitorCandidate = ""
		return
	}
	if best == d.preferredMonitor || current-lowest < 15 {
		d.monitorCandidate = ""
		return
	}
	if d.monitorCandidate != best {
		d.monitorCandidate = best
		d.candidateSince = now
		return
	}
	if now.Sub(d.candidateSince) >= 3*time.Minute && now.Sub(d.preferredSince) >= 3*time.Minute {
		d.preferredMonitor = best
		d.preferredSince = now
		d.monitorCandidate = ""
	}
}
func (d *Dashboard) agentWeight(node string, now time.Time) string {
	d.mu.RLock()
	defer d.mu.RUnlock()
	if d.preferredMonitor == "" || now.Sub(d.snapshot.At) > staleAfter {
		return "100%\n"
	}
	if node == d.preferredMonitor {
		return "100%\n"
	}
	return "10%\n"
}
func (d *Dashboard) monitorAgent(ctx context.Context) {
	listener, err := net.Listen("tcp", net.JoinHostPort(d.member.Private, "8012"))
	if err != nil {
		fmt.Fprintln(os.Stderr, "Monitoring preference agent unavailable")
		return
	}
	defer listener.Close()
	go func() { <-ctx.Done(); listener.Close() }()
	for {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		func() {
			defer conn.Close()
			conn.SetDeadline(time.Now().Add(time.Second))
			host, _, err := net.SplitHostPort(conn.RemoteAddr().String())
			allowed := false
			for _, m := range d.config.Members {
				if host == m.Private {
					allowed = true
				}
			}
			if err != nil || !allowed {
				return
			}
			raw := make([]byte, 64)
			count, err := conn.Read(raw)
			if err != nil {
				return
			}
			node := strings.TrimSpace(string(raw[:count]))
			valid := false
			for _, m := range d.config.Members {
				if node == m.ID {
					valid = true
				}
			}
			if !valid {
				return
			}
			io.WriteString(conn, d.agentWeight(node, time.Now()))
		}()
	}
}
