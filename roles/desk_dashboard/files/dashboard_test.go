package main

import (
	"bytes"
	"encoding/json"
	"math"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestHostCPUExcludesGuestAndIOWait(t *testing.T) {
	a := parseCPU("cpu 10 0 10 60 20 0 0 0 100 0\n")
	b := parseCPU("cpu 20 0 20 120 40 0 0 0 200 0\n")
	value := cpuPercent(a, b)
	if value == nil || *value != 20 {
		t.Fatalf("expected 20%% host CPU, got %v", value)
	}
	if cpuPercent(cpuSample{}, b) != nil || cpuPercent(b, a) != nil {
		t.Fatal("first/reset samples must be unknown")
	}
}
func TestMemoryUsesAvailable(t *testing.T) {
	v := memoryPercent("MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 700 kB\n")
	if v == nil || *v != 30 {
		t.Fatal("cache is reclaimable, expected 30%")
	}
}
func TestActivityRejectsStaleWrongBootAndNode(t *testing.T) {
	v := map[string]any{"protocol": "xiaozhi-worker-activity-v1", "worker_id": "deskb1x", "boot_id": "boot", "instance": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "monotonic": float64(99)}
	if !freshActivity(v, "boot", "deskb1x", "xiaozhi-worker-activity-v1", 3, 100) {
		t.Fatal("fresh fixture rejected")
	}
	for _, args := range [][2]string{{"old", "deskb1x"}, {"boot", "deskb2x"}} {
		if freshActivity(v, args[0], args[1], "xiaozhi-worker-activity-v1", 3, 100) {
			t.Fatal("invalid owner accepted")
		}
	}
	v["monotonic"] = float64(100.005)
	if !freshActivity(v, "boot", "deskb1x", "xiaozhi-worker-activity-v1", 3, 100) {
		t.Fatal("clock rounding must not discard fresh metadata")
	}
	v["monotonic"] = float64(99)
	if freshActivity(v, "boot", "deskb1x", "xiaozhi-worker-activity-v1", 3, 104) {
		t.Fatal("stale activity accepted")
	}
}
func TestOnlyConfiguredPeersCanReadAndNoWrites(t *testing.T) {
	d := &Dashboard{config: Config{Members: []Member{{ID: "deskb1x", Private: "10.10.10.11"}}}}
	for _, fixture := range []struct {
		method, path, remote string
		code                 int
	}{{"GET", "/api/cluster/status", "192.168.1.23:1234", 403}, {"POST", "/api/cluster/status", "10.10.10.11:1234", 405}, {"GET", "/api/cluster/status?url=http://other", "10.10.10.11:1234", 400}, {"GET", "/local", "10.10.10.11:1234", 200}, {"GET", "/other", "10.10.10.11:1234", 404}} {
		req := httptest.NewRequest(fixture.method, fixture.path, nil)
		req.RemoteAddr = fixture.remote
		response := httptest.NewRecorder()
		d.handle(response, req)
		if response.Code != fixture.code {
			t.Fatalf("%s %s got %d", fixture.method, fixture.path, response.Code)
		}
	}
}
func TestUnavailableNodeCannotKeepVIPOrActivity(t *testing.T) {
	m := Member{ID: "deskb1x", Management: "192.168.1.101"}
	n := unknown(m)
	if n.VIP["dns"] != nil || n.VIP["xiaozhi"] != nil || n.MQTT != nil || n.Activity["llm"] != nil {
		t.Fatal("unknown node claims ownership")
	}
	n.Online = true
	n.At = time.Now().Add(-21 * time.Second)
	if validateNode(n, m, time.Now()) {
		t.Fatal("stale node accepted")
	}
}
func TestEventsAreTransitionsBoundedAndSkipVoiceNoise(t *testing.T) {
	d := &Dashboard{config: Config{Node: "deskb1x"}, started: time.Now()}
	m := Member{ID: "deskb1x"}
	a := unknown(m)
	a.Online = true
	a.Components["core"] = "ready"
	b := a
	b.Activity = map[string]*int{}
	v := 1
	b.Activity["llm"] = &v
	d.observe(a, b)
	if len(d.events) != 0 {
		t.Fatal("voice job must not create cluster events")
	}
	for i := 0; i < 80; i++ {
		d.event(a, "node", "Node telemetry restored")
	}
	if len(d.events) != 64 {
		t.Fatal("history not bounded")
	}
	b.Online = false
	d.observe(a, b)
	if d.events[63].Message != "Node telemetry unavailable" {
		t.Fatal("missing unavailability event")
	}
}

func TestEventReplicationIsBoundedAndDeduplicatesObservers(t *testing.T) {
	now := time.Now()
	events := []Event{{ID: "a", At: now, Node: "deskb2x", Kind: "node", Message: "Node telemetry unavailable"}, {ID: "b", At: now.Add(-time.Second), Node: "deskb2x", Kind: "node", Message: "Node telemetry unavailable"}, {ID: "a", At: now, Node: "deskb2x", Kind: "node", Message: "Node telemetry unavailable"}, {ID: "old", At: now.Add(-7 * time.Hour), Node: "deskb1x", Kind: "ownership", Message: "DNS VIP acquired"}}
	if got := mergeEvents(events, now); len(got) != 1 {
		t.Fatalf("expected one recent logical event, got %d", len(got))
	}
}

func TestRefreshingDesiredStateStillRequiresAValidatedSnapshot(t *testing.T) {
	cp := map[string]any{"protocol": "xiaozhi-control-plane-v1", "node_id": "deskb3x",
		"configuration":  map[string]any{"desired_revision": float64(11), "sync_status": "synced"},
		"reconciliation": map[string]any{"state": "refreshing", "last_success_at": time.Now().Add(-time.Minute).Format(time.RFC3339Nano)}}
	if desiredSync(cp, "deskb3x").State != "synced" {
		t.Fatal("validated snapshot was lost during refresh")
	}
	r := object(cp["reconciliation"])
	r["last_success_at"] = time.Now().Add(-10 * time.Minute).Format(time.RFC3339Nano)
	if desiredSync(cp, "deskb3x").State != "pending" {
		t.Fatal("stale reconciliation accepted")
	}
	r["error_code"] = "cloud_refresh_failed"
	if desiredSync(cp, "deskb3x").State != "failed" {
		t.Fatal("failed refresh accepted")
	}
}

func panelFixture(now time.Time) (Snapshot, []Member) {
	members := []Member{{ID: "deskb1x", Management: "192.168.1.101"}, {ID: "deskb2x", Management: "192.168.1.102"}, {ID: "deskb3x", Management: "192.168.1.103"}}
	zero, one := 0, 1
	yes, no := true, false
	cpu := 11.12345
	s := Snapshot{Protocol: protocol, At: now, Events: []Event{}, Firmware: Firmware{At: now, State: "offline"}}
	for i, m := range members {
		n := unknown(m)
		n.Online = true
		n.At = now
		n.Boot = "not-for-panel"
		n.CPU = &cpu
		n.VIP["xiaozhi"] = &no
		n.VIP["dns"] = &no
		if i == 0 {
			n.VIP["xiaozhi"] = &yes
		}
		if i == 1 {
			n.VIP["dns"] = &yes
		}
		n.Activity = map[string]*int{"asr": &zero, "llm": &zero, "tts": &one}
		n.Core = &zero
		n.MQTT = &zero
		n.Components = map[string]string{"worker": "ready", "core": "ready", "mqtt": "ready", "dns": "ready"}
		n.DNSHealth = DNSHealth{At: now, Mode: "adguard", Backends: []string{"deskb1x", "deskb2x", "deskb3x"}}
		s.Nodes = append(s.Nodes, n)
	}
	return s, members
}
func TestPanelUsesCachedBoundedProjectionWithoutPrivateMetadata(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	for i := 0; i < 64; i++ {
		s.Events = append(s.Events, Event{At: now, Node: "deskb1x", Kind: "node", Message: strings.Repeat("x", 256)})
	}
	p := panelSnapshot(s, members, now)
	if p.State != "ready" || p.Protocol != panelProtocol || len(p.Nodes) != 3 || len(p.Events) != 3 || *p.Owners["dns"].Node != "deskb2x" {
		t.Fatalf("invalid compact projection: %+v", p)
	}
	if *p.Nodes[0].CPU != 11.1 || *p.Nodes[0].Jobs.TTS != 1 {
		t.Fatal("metrics or actual job count lost")
	}
	raw, e := json.Marshal(p)
	if e != nil || len(raw) > panelMaxBytes {
		t.Fatal("panel exceeds device buffer")
	}
	for _, secret := range []string{"not-for-panel", "history_since", "observer", "worker_revision", "private", "client_url"} {
		if bytes.Contains(raw, []byte(secret)) {
			t.Fatalf("unnecessary private metadata %s", secret)
		}
	}
	// No HTTP client is provided: endpoint must not launch probes or peer reads.
	d := &Dashboard{config: Config{Members: members}, snapshot: s}
	r := httptest.NewRequest("GET", "/api/cluster/panel", nil)
	r.RemoteAddr = "127.0.0.1:1"
	w := httptest.NewRecorder()
	d.handle(w, r)
	if w.Code != 200 || w.Header().Get("Content-Type") != "application/json" || w.Header().Get("Cache-Control") != "no-store" || len(w.Body.Bytes()) > panelMaxBytes {
		t.Fatal("invalid cached panel response")
	}
}
func TestPanelStaleSnapshotClearsAllLiveClaims(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now.Add(-21 * time.Second))
	p := panelSnapshot(s, members, now)
	if p.State != "unavailable" || p.Owners["dns"].Node != nil || len(p.DNS.Eligible) != 0 || len(p.Events) != 0 {
		t.Fatal("stale global snapshot claims routing")
	}
	for _, n := range p.Nodes {
		if n.Online || n.CPU != nil || n.Jobs.TTS != nil || n.DNS != nil || n.VIP["xiaozhi"] != nil {
			t.Fatal("stale node values retained")
		}
	}
	d := &Dashboard{config: Config{Members: members}, snapshot: s}
	r := httptest.NewRequest("GET", "/api/cluster/panel", nil)
	r.RemoteAddr = "127.0.0.1:1"
	w := httptest.NewRecorder()
	d.handle(w, r)
	if w.Code != 503 || !strings.Contains(w.Body.String(), panelProtocol) {
		t.Fatal("stale response must be structured 503")
	}
}
func TestPanelPartialFailureAndConflictDoNotInventOwners(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	s.Nodes[1].At = now.Add(-21 * time.Second)
	p := panelSnapshot(s, members, now)
	if p.State != "degraded" || p.Nodes[1].Online || p.Nodes[1].DNS != nil || p.Owners["dns"].State != "unknown" || p.DNS.Mode != "unknown" {
		t.Fatal("unavailable DNS owner retained")
	}
	s, members = panelFixture(now)
	yes := true
	s.Nodes[2].VIP["dns"] = &yes
	p = panelSnapshot(s, members, now)
	if p.Owners["dns"].State != "conflict" || p.Owners["dns"].Node != nil || len(p.DNS.Eligible) != 0 {
		t.Fatal("duplicate owners silently selected")
	}
}
func TestPanelDNSFallbackAndStaleHealth(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	s.Nodes[1].DNSHealth.Mode = "fallback"
	s.Nodes[1].DNSHealth.Resolver = "1.1.1.1"
	p := panelSnapshot(s, members, now)
	if p.DNS.Mode != "fallback" || p.State != "degraded" || len(p.DNS.Eligible) != 0 || p.DNS.Resolver == nil {
		t.Fatal("fallback not explicit")
	}
	for _, n := range p.Nodes {
		if n.DNS == nil || *n.DNS {
			t.Fatal("fallback falsely shows filtering backends")
		}
	}
	s.Nodes[1].DNSHealth.At = now.Add(-31 * time.Second)
	p = panelSnapshot(s, members, now)
	if p.DNS.Mode != "unknown" || p.DNS.Resolver != nil {
		t.Fatal("stale DNS health retained")
	}
}
func TestPanelRouteRejectsWritesQueriesAndOtherClients(t *testing.T) {
	d := &Dashboard{}
	for _, f := range []struct {
		method, path, remote string
		code                 int
	}{{"POST", "/api/cluster/panel", "127.0.0.1:1", 405}, {"GET", "/api/cluster/panel?node=deskb1x", "127.0.0.1:1", 400}, {"GET", "/api/cluster/panel", "192.168.1.23:1", 403}, {"GET", "/api/cluster/panel/extra", "127.0.0.1:1", 404}} {
		r := httptest.NewRequest(f.method, f.path, nil)
		r.RemoteAddr = f.remote
		w := httptest.NewRecorder()
		d.handle(w, r)
		if w.Code != f.code {
			t.Fatalf("%s %s got %d", f.method, f.path, w.Code)
		}
	}
}

func TestPanelMetricRoundingCannotOverflow(t *testing.T) {
	value := math.MaxFloat64
	got := panelMetric(&value)
	if got == nil || math.IsInf(*got, 0) || math.IsNaN(*got) {
		t.Fatal("finite source must remain serializable")
	}
}
