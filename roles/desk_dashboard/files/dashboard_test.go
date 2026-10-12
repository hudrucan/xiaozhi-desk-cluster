package main

import (
	"net/http/httptest"
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
