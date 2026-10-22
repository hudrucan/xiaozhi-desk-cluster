package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestNotificationEventsRemainBoundedReadOnlyPanelEvents(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	meta := map[string]any{"observed_at": now.Unix(), "events": []map[string]any{{"id": "abcDEF123456", "time": now.Unix(), "message": "Door opened"}}}
	events := notificationEvents(meta, "deskb1x", now)
	if len(events) != 1 || events[0].Kind != "notification" || events[0].Node != "deskb1x" {
		t.Fatal(events)
	}
	cluster := Event{ID: "cluster", At: now.Add(-time.Second), Node: "deskb2x", Kind: "ownership", Message: "VIP moved"}
	merged := mergeEvents(append(events, events[0], cluster), now)
	if len(merged) != 2 {
		t.Fatal("notification dedup must retain cluster events", merged)
	}
	snapshot, members := panelFixture(now)
	snapshot.Events = merged
	projected := panelSnapshot(snapshot, members, now)
	if len(projected.Events) != 2 || projected.Events[0].Kind != "notification" || projected.Events[1].Kind != "ownership" || projected.PollMS != 5000 {
		t.Fatal("Pi event or polling contract changed", projected)
	}
	raw, err := json.Marshal(projected)
	if err != nil || len(raw) > panelMaxBytes {
		t.Fatal("Pi payload exceeds contract")
	}
	if len(notificationEvents(meta, "deskb1x", now.Add(21*time.Second))) != 0 {
		t.Fatal("stale metadata retained")
	}
	meta["events"] = []map[string]any{{"id": "abcDEF123456", "time": now.Unix(), "message": strings.Repeat("x", 257)}}
	if len(notificationEvents(meta, "deskb1x", now)) != 0 {
		t.Fatal("oversize notification retained")
	}
}

func TestNtfyHealthProjectionExpiresWithoutChangingPiEventsContract(t *testing.T) {
	now := time.Now().UTC()
	snapshot, members := panelFixture(now)
	snapshot.Nodes[0].Components["ntfy"] = "ready"
	snapshot.Nodes[1].Components["ntfy"] = "unavailable"
	p := panelSnapshot(snapshot, members, now)
	if p.Nodes[0].Components["ntfy"] != "ready" || p.Nodes[1].Components["ntfy"] != "unavailable" || p.Nodes[2].Components["ntfy"] != "unknown" || p.Owners["xiaozhi"].Node == nil || p.PollMS != 5000 {
		t.Fatal("ntfy readiness, shared VIP or polling lost", p)
	}
	snapshot.At = now.Add(-staleAfter - time.Second)
	p = panelSnapshot(snapshot, members, now)
	if p.Nodes[0].Components["ntfy"] != "unknown" || p.Owners["xiaozhi"].Node != nil {
		t.Fatal("stale ntfy readiness or VIP retained")
	}
	raw, err := json.Marshal(p)
	if err != nil || len(raw) > panelMaxBytes {
		t.Fatal("Pi payload contract exceeded")
	}
}

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
	now := time.Now()
	d.observeAt(a, b, now)
	d.observeAt(b, b, now.Add(telemetryEventDelay))
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

func TestTimelineSuppressesTransientPendingAndServiceFlaps(t *testing.T) {
	now := time.Now()
	s, _ := panelFixture(now)
	a := s.Nodes[0]
	revision := 11
	a.Config["xiaozhi"] = SyncStatus{State: "synced", Revision: &revision}
	d := &Dashboard{config: Config{Node: a.ID}, started: now}
	b := a
	b.At = now.Add(5 * time.Second)
	b.Config = map[string]SyncStatus{"xiaozhi": {State: "pending", Revision: &revision}}
	b.Components = map[string]string{"worker": "unavailable"}
	d.observe(a, b)
	c := a
	c.At = now.Add(10 * time.Second)
	d.observe(b, c)
	if len(d.events) != 0 {
		t.Fatalf("transient refresh/health created noise: %+v", d.events)
	}
	b.At = now.Add(15 * time.Second)
	d.observe(c, b)
	b.At = now.Add(45 * time.Second)
	d.observe(b, b)
	if len(d.events) != 2 {
		t.Fatalf("persistent failures must be logged: %+v", d.events)
	}
	for _, e := range d.events {
		if e.From == "" || e.To == "" || e.Subject == "" {
			t.Fatal("transition missing context")
		}
	}
	c.At = now.Add(50 * time.Second)
	d.observe(b, c)
	if len(d.events) != 4 {
		t.Fatal("recovery must follow published failures")
	}
}
func TestTimelineNewRevisionAndFailureAreImmediate(t *testing.T) {
	now := time.Now()
	s, _ := panelFixture(now)
	a := s.Nodes[0]
	one, two := 11, 12
	a.Config["xiaozhi"] = SyncStatus{State: "synced", Revision: &one}
	b := a
	b.Config = map[string]SyncStatus{"xiaozhi": {State: "pending", Revision: &two}}
	d := &Dashboard{config: Config{Node: a.ID}, started: now}
	d.observe(a, b)
	if len(d.events) != 1 || !strings.Contains(d.events[0].From, "revision 11") || !strings.Contains(d.events[0].To, "revision 12") {
		t.Fatal("new revision missing")
	}
	c := b
	c.Config = map[string]SyncStatus{"xiaozhi": {State: "failed", Revision: &two}}
	d.observe(b, c)
	if len(d.events) != 2 {
		t.Fatal("explicit apply failure must be immediate")
	}
}
func TestTimelineDetectsRebootAfterUnavailableSample(t *testing.T) {
	now := time.Now()
	s, m := panelFixture(now)
	a := s.Nodes[0]
	d := &Dashboard{config: Config{Node: a.ID}, started: now}
	d.observe(Node{}, a)
	offline := unknown(m[0])
	d.observe(a, offline)
	b := a
	b.Boot = "different-boot"
	d.observe(offline, b)
	reboot := 0
	for _, e := range d.events {
		if e.Message == "Node reboot detected" {
			reboot++
			if strings.Contains(e.From, a.Boot) || strings.Contains(e.To, b.Boot) {
				t.Fatal("boot identities leaked")
			}
		}
	}
	if reboot != 1 {
		t.Fatal("reboot lost across telemetry interruption")
	}
}

func TestTelemetryEventDebounceUsesObserverTime(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	a := s.Nodes[0]
	offline := unknown(members[0])
	offline.At = now.Add(-time.Hour) // Retained stale time must not short-circuit debounce.
	d := &Dashboard{config: Config{Node: "deskb2x"}, started: now}
	d.observeAt(a, offline, now)
	d.observeAt(offline, offline, now.Add(25*time.Second))
	if len(d.events) != 0 || offline.Online {
		t.Fatal("brief loss logged, or health made falsely available")
	}
	d.observeAt(offline, a, now.Add(26*time.Second))
	if len(d.events) != 0 {
		t.Fatal("unpublished dip emitted recovery")
	}
	// A new loss starts its own window, rather than accumulating separate dips.
	d.observeAt(a, offline, now.Add(35*time.Second))
	d.observeAt(offline, offline, now.Add(60*time.Second))
	if len(d.events) != 0 {
		t.Fatal("debounce did not reset after healthy sample")
	}
	d.observeAt(offline, offline, now.Add(65*time.Second))
	d.observeAt(offline, offline, now.Add(70*time.Second))
	if len(d.events) != 1 || d.events[0].From != "Online" || d.events[0].To != "Unavailable" {
		t.Fatalf("persistent loss not logged exactly once: %+v", d.events)
	}
	d.observeAt(offline, a, now.Add(75*time.Second))
	d.observeAt(a, a, now.Add(80*time.Second))
	if len(d.events) != 2 || d.events[1].Message != "Node telemetry restored" {
		t.Fatal("confirmed recovery not logged exactly once")
	}
}
func TestInitialDiscoveryDoesNotCreateTelemetryRecovery(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	a := s.Nodes[0]
	d := &Dashboard{config: Config{Node: "deskb2x"}, started: now}
	u := unknown(members[0])
	d.observeAt(Node{}, u, now)
	d.observeAt(u, u, now.Add(time.Minute))
	d.observeAt(u, a, now.Add(2*time.Minute))
	if len(d.events) != 0 {
		t.Fatalf("initial discovery created false recovery: %+v", d.events)
	}
}
func TestCollectorRestartDistinguishedFromNodeReboot(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	a := s.Nodes[0]
	first, second, third := now.Add(-time.Hour), now.Add(-time.Minute), now
	a.CollectorStarted = &first
	d := &Dashboard{config: Config{Node: "deskb2x"}, started: now}
	d.observeAt(Node{}, a, now)
	u := unknown(members[0])
	d.observeAt(a, u, now.Add(5*time.Second))
	b := a
	b.CollectorStarted = &second
	d.observeAt(u, b, now.Add(25*time.Second))
	d.observeAt(b, b, now.Add(26*time.Second))
	if len(d.events) != 1 || d.events[0].Subject != "telemetry_collector" || d.events[0].Message != "Telemetry collector restarted" {
		t.Fatalf("brief collector restart mislabeled: %+v", d.events)
	}
	c := b
	c.Boot = "new-boot"
	c.CollectorStarted = &third
	d.observeAt(b, c, now.Add(time.Minute))
	if len(d.events) != 2 || d.events[1].Message != "Node reboot detected" {
		t.Fatalf("node reboot mislabeled as collector restart: %+v", d.events)
	}
}
func TestCollectorMetadataValidationAndLegacyCompatibility(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	n := s.Nodes[0]
	if !validateNode(n, members[0], now) {
		t.Fatal("legacy node without collector timestamp rejected")
	}
	started := now.Add(-24 * time.Hour)
	n.CollectorStarted = &started
	if !validateNode(n, members[0], now) {
		t.Fatal("valid long-running collector rejected")
	}
	future := now.Add(time.Minute)
	n.CollectorStarted = &future
	if validateNode(n, members[0], now) {
		t.Fatal("future collector timestamp accepted")
	}
	zero := time.Time{}
	n.CollectorStarted = &zero
	if validateNode(n, members[0], now) {
		t.Fatal("invalid zero collector timestamp accepted")
	}
}

type unavailableTelemetryTransport struct{}

func (unavailableTelemetryTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("simulated peer telemetry timeout")
}
func TestTelemetryDebounceDoesNotRetainStaleOwnerOrActivity(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	d := &Dashboard{config: Config{Node: members[2].ID, Members: members, Port: 8010},
		started: now, local: s.Nodes[2], snapshot: s,
		client: &http.Client{Transport: unavailableTelemetryTransport{}}}
	d.aggregate(context.Background())
	for _, n := range d.snapshot.Nodes[:2] {
		if n.Online || n.VIP["dns"] != nil || n.VIP["xiaozhi"] != nil || n.Activity["tts"] != nil {
			t.Fatalf("failed peer still claims live state during debounce: %+v", n)
		}
	}
	if len(d.snapshot.Events) != 0 {
		t.Fatal("first failed read created timeline noise")
	}
	p := panelSnapshot(d.snapshot, members, time.Now())
	if p.Owners["dns"].Node != nil || p.Owners["xiaozhi"].Node != nil || p.State != "degraded" {
		t.Fatal("compact API retained stale ownership during debounce")
	}
}

func TestVPNReadinessRejectsStaleWrongBootAndMalformedSnapshots(t *testing.T) {
	now := time.Now()
	v := map[string]any{"protocol": "desk-vpn-status-v1", "node": "deskb1x", "boot_id": "boot", "observed_at": now.Format(time.RFC3339Nano), "ready": true}
	if r := vpnReadiness(v, "boot", "deskb1x", now); r == nil || !*r {
		t.Fatal("fresh probe missing")
	}
	for key, bad := range map[string]any{"node": "deskb2x", "boot_id": "old", "protocol": "other", "ready": "true", "observed_at": now.Add(-21 * time.Second).Format(time.RFC3339Nano)} {
		original := v[key]
		v[key] = bad
		if vpnReadiness(v, "boot", "deskb1x", now) != nil {
			t.Fatalf("accepted invalid %s", key)
		}
		v[key] = original
	}
	v["ready"] = false
	if r := vpnReadiness(v, "boot", "deskb1x", now); r == nil || *r {
		t.Fatal("failed probe became ready")
	}
}
func TestPanelVPNOwnerProjectionAndConflict(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	yes, no := true, false
	for i := range s.Nodes {
		s.Nodes[i].VIP["vpn"] = &no
		s.Nodes[i].VPNReady = &yes
	}
	s.Nodes[2].VIP["vpn"] = &yes
	p := panelSnapshot(s, members, now)
	if p.Owners["vpn"].Node == nil || *p.Owners["vpn"].Node != "deskb3x" || p.Nodes[2].VPNReady == nil || !*p.Nodes[2].VPNReady {
		t.Fatal("VPN owner lost")
	}
	s.Nodes[0].VIP["vpn"] = &yes
	if panelSnapshot(s, members, now).Owners["vpn"].State != "conflict" {
		t.Fatal("duplicate VPN owner hidden")
	}
}

func TestPanelMonitoringPreferenceUsesFreshUIOwnerAndReadyTarget(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	s.Nodes[0].MonitoringPreferred = "deskb3x"
	s.Nodes[1].MonitoringPreferred = "deskb2x"
	s.Nodes[2].MonitoringReady = true
	p := panelSnapshot(s, members, now)
	if p.MonitoringPreferred == nil || *p.MonitoringPreferred != "deskb3x" || p.Nodes[2].MonitoringReady == nil || !*p.Nodes[2].MonitoringReady {
		t.Fatal("Monitoring preference must come from the current UI owner")
	}
	s.Nodes[2].MonitoringReady = false
	if panelSnapshot(s, members, now).MonitoringPreferred != nil {
		t.Fatal("Unavailable hub advertised as preferred")
	}
	s.Nodes[2].MonitoringReady = true
	s.Nodes[0].At = now.Add(-time.Minute)
	if panelSnapshot(s, members, now).MonitoringPreferred != nil {
		t.Fatal("Stale UI owner retained monitoring preference")
	}
	s.Nodes[0].At = now
	s.Nodes[2].At = now.Add(-time.Minute)
	if panelSnapshot(s, members, now).MonitoringPreferred != nil {
		t.Fatal("Stale target advertised as preferred")
	}
	s.Nodes[2].At = now
	yes := true
	s.Nodes[1].VIP["xiaozhi"] = &yes
	if panelSnapshot(s, members, now).MonitoringPreferred != nil {
		t.Fatal("Conflicting UI owners advertised a monitoring preference")
	}
}

func TestPanelNetworkProjectionKeepsFreshOwnerAndBoundedReplicaMetadata(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	for i := range s.Nodes {
		state := "standby"
		if i == 0 {
			state = "active"
		}
		s.Nodes[i].Network = &NetworkStatus{State: state, Sync: "synced", At: now.Add(-time.Second),
			Subnet: "192.168.1.0/24", ScanMinutes: 1, ScanSchedule: "*/1 * * * *", VoterReady: true,
			Revision: &NetworkRevision{Term: 33, Node: "deskb1x", Sequence: 40}}
	}
	p := panelSnapshot(s, members, now)
	if p.Owners["network"].Node == nil || *p.Owners["network"].Node != "deskb1x" || p.Nodes[1].Network.State != "standby" || *p.Nodes[0].Network.AgeMS != 1000 {
		t.Fatalf("Network owner or replica data lost: %+v", p)
	}
	for i := 0; i < 3; i++ {
		s.Events = append(s.Events, Event{At: now, Node: "deskb1x", Kind: "node", Message: strings.Repeat("x", 256)})
		s.Nodes[i].Network.ScanSchedule = strings.Repeat("*", 128)
	}
	raw, err := json.Marshal(panelSnapshot(s, members, now))
	if err != nil || len(raw) > panelMaxBytes {
		t.Fatal("Network exceeded the existing 8 KiB panel budget")
	}
	s.Nodes[1].Network.State = "active"
	if panelSnapshot(s, members, now).Owners["network"].State != "conflict" {
		t.Fatal("Duplicate Network owner hidden")
	}
	s.Nodes[1].Network.State = "standby"
	s.Nodes[0].Network.VoterReady = false
	if panelSnapshot(s, members, now).Owners["network"].State != "unknown" {
		t.Fatal("Warming owner advertised")
	}
	s.Nodes[0].Network.VoterReady = true
	s.Nodes[0].Network.At = now.Add(-15 * time.Second)
	p = panelSnapshot(s, members, now)
	if p.Owners["network"].State != "unknown" || p.Nodes[0].Network != nil || !p.Nodes[0].Online {
		t.Fatal("Stale Network must disappear without hiding healthy node telemetry")
	}
	s.Nodes[0].At = now.Add(-time.Minute)
	s.Nodes[0].Network.At = now
	if panelSnapshot(s, members, now).Nodes[0].Network != nil {
		t.Fatal("Offline node retained Network claims")
	}
}

func TestValidateNodeAcceptsVPNComponentAndRejectsUnknownFields(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	n := s.Nodes[0]
	for _, state := range []string{"ready", "unavailable", "unknown"} {
		n.Components["vpn"] = state
		if !validateNode(n, members[0], now) {
			t.Fatalf("valid VPN component %s rejected", state)
		}
	}
	n.Components["vpn"] = "unvalidated"
	if validateNode(n, members[0], now) {
		t.Fatal("arbitrary VPN state accepted")
	}
	delete(n.Components, "vpn")
	n.Components["unexpected"] = "ready"
	if validateNode(n, members[0], now) {
		t.Fatal("unknown component accepted")
	}
}

func TestHealthRequiresValidLocalSnapshotButSurvivesPeerOutage(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	d := &Dashboard{config: Config{Members: members}, member: members[0], local: s.Nodes[0], snapshot: s}
	d.local.Components["vpn"] = "ready"
	request := func() int {
		r := httptest.NewRequest("GET", "/health", nil)
		r.RemoteAddr = members[0].Private + ":1234"
		w := httptest.NewRecorder()
		d.handle(w, r)
		return w.Code
	}
	if request() != 200 {
		t.Fatal("valid VPN collector not ready")
	}
	d.snapshot.Nodes[1] = unknown(members[1])
	d.snapshot.Nodes[2] = unknown(members[2])
	if request() != 200 {
		t.Fatal("peer outage removed healthy dashboard from HA")
	}
	d.local.Components["vpn"] = "unexpected"
	if request() != 503 {
		t.Fatal("self-rejected telemetry passed rollout health")
	}
}

func TestHomeAssistantMetadata(t *testing.T) {
	now := time.Now()
	meta := map[string]any{"protocol": "desk-homeassistant-v1", "node": "deskb1x", "observed_at": float64(now.Unix()), "checkpoint_at": float64(now.Add(-time.Minute).Unix()), "state": "active", "ready": true, "sync": "synced", "checkpoint_revision": float64(2), "pending_disk_changes": true}
	h := homeAssistantMetadata(meta, "deskb1x", now)
	if h == nil || h.Revision != 2 || !h.Ready || h.Pending == nil || !*h.Pending {
		t.Fatal("valid state lost")
	}
	for _, key := range []string{"protocol", "node", "state", "checkpoint_revision", "observed_at", "ready"} {
		copy := map[string]any{}
		for k, v := range meta {
			copy[k] = v
		}
		copy[key] = "invalid"
		if homeAssistantMetadata(copy, "deskb1x", now) != nil {
			t.Fatal("invalid metadata accepted", key)
		}
	}
	meta["state"] = "standby"
	if homeAssistantMetadata(meta, "deskb1x", now) != nil {
		t.Fatal("standby claimed ready")
	}
	meta["ready"] = false
	meta["observed_at"] = float64(now.Add(-20 * time.Second).Unix())
	if homeAssistantMetadata(meta, "deskb1x", now) != nil {
		t.Fatal("stale metadata accepted")
	}
}

func TestPanelHomeAssistantProjectionFreshnessConflictAndBudget(t *testing.T) {
	now := time.Now()
	s, members := panelFixture(now)
	pending := true
	for i := range s.Nodes {
		s.Nodes[i].HomeAssistant = &HomeAssistantStatus{State: "standby", Sync: "synced", Revision: 9, At: now, CheckpointAt: now.Add(-time.Minute)}
	}
	s.Nodes[2].HomeAssistant.State = "active"
	s.Nodes[2].HomeAssistant.Ready = true
	s.Nodes[2].HomeAssistant.Pending = &pending
	p := panelSnapshot(s, members, now)
	if p.Owners["homeassistant"].Node == nil || *p.Owners["homeassistant"].Node != "deskb3x" || p.Nodes[2].HomeAssistant.Pending == nil || !*p.Nodes[2].HomeAssistant.Pending {
		t.Fatal("HA owner/pending lost")
	}
	raw, err := json.Marshal(p)
	if err != nil || len(raw) > panelMaxBytes {
		t.Fatal("HA projection exceeds bounded API")
	}
	s.Nodes[1].HomeAssistant.State = "starting"
	if panelSnapshot(s, members, now).Owners["homeassistant"].State != "conflict" {
		t.Fatal("Conflicting HA ownership hidden")
	}
	s.Nodes[1].HomeAssistant.State = "standby"
	s.Nodes[2].HomeAssistant.At = now.Add(-15 * time.Second)
	p = panelSnapshot(s, members, now)
	if p.Nodes[2].HomeAssistant != nil || p.Owners["homeassistant"].State != "unknown" || !p.Nodes[2].Online {
		t.Fatal("HA freshness must expire independently")
	}
	s.Nodes[2].HomeAssistant.At = now
	s.Nodes[2].At = now.Add(-time.Minute)
	if panelSnapshot(s, members, now).Nodes[2].HomeAssistant != nil {
		t.Fatal("Offline node retained HA claim")
	}
}
