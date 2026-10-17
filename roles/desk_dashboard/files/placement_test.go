package main

import (
	"strings"
	"testing"
	"time"
)

func readyMonitor(id string, cpu float64, now time.Time) Node {
	return Node{ID: id, Online: true, At: now, CPU: &cpu, MonitoringReady: true, VIP: map[string]*bool{}, Activity: map[string]*int{}}
}
func TestMonitoringPreferenceAvoidsUIOwnerAndHolds(t *testing.T) {
	now := time.Now()
	a := readyMonitor("deskb1x", 10, now)
	b := readyMonitor("deskb2x", 5, now)
	yes := true
	b.VIP["xiaozhi"] = &yes
	d := &Dashboard{}
	d.selectMonitoring([]Node{a, b}, now)
	if d.preferredMonitor != a.ID {
		t.Fatal("UI owner selected despite higher role cost")
	}
	a.CPU = func() *float64 { x := 40.; return &x }()
	d.selectMonitoring([]Node{a, b}, now.Add(time.Second))
	if d.preferredMonitor != a.ID {
		t.Fatal("Preference changed on a short spike")
	}
	a.At = now.Add(181 * time.Second)
	b.At = a.At
	d.selectMonitoring([]Node{a, b}, a.At)
	if d.preferredMonitor != b.ID {
		t.Fatal("Sustained improvement did not change preference")
	}
}
func TestMonitoringPreferenceRejectsUnavailableAndMaintenance(t *testing.T) {
	now := time.Now()
	a := readyMonitor("deskb1x", 1, now)
	b := readyMonitor("deskb2x", 20, now)
	d := &Dashboard{}
	d.selectMonitoring([]Node{a, b}, now)
	yes := true
	a.Admission = map[string]*bool{"worker": &yes}
	d.selectMonitoring([]Node{a, b}, now)
	if d.preferredMonitor != b.ID {
		t.Fatal("Maintenance node retained")
	}
	b.At = now.Add(-time.Minute)
	d.selectMonitoring([]Node{a, b}, now)
	if d.preferredMonitor != "" {
		t.Fatal("Stale preference advertised")
	}
}

func TestMonitoringSpreadsStackedNetworkAndHomeAssistantOwners(t *testing.T) {
	now := time.Now()
	nodes := []Node{readyMonitor("deskb1x", 22, now), readyMonitor("deskb2x", 20, now), readyMonitor("deskb3x", 21, now)}
	yes := true
	nodes[0].VIP["vpn"] = &yes
	nodes[0].ServiceRoles = map[string]string{"network": "active"}
	nodes[1].VIP["xiaozhi"] = &yes
	nodes[2].VIP["dns"] = &yes
	nodes[2].HomeAssistant = &HomeAssistantStatus{State: "active", Sync: "synced", Ready: true, At: now, CheckpointAt: now}
	d := &Dashboard{preferredMonitor: "deskb1x", preferredSince: now.Add(-time.Hour)}
	d.selectMonitoring(nodes, now)
	if d.preferredMonitor != "deskb1x" || d.monitorCandidate != "deskb2x" {
		t.Fatal("stacked owner did not yield a stable candidate")
	}
	for i := range nodes {
		nodes[i].At = now.Add(3 * time.Minute)
	}
	nodes[2].HomeAssistant.At = nodes[2].At
	d.selectMonitoring(nodes, nodes[0].At)
	if d.preferredMonitor != "deskb2x" {
		t.Fatal("monitoring stayed on VPN + Network instead of the single-role node")
	}
	// If the less-stacked hub becomes unhealthy, stacked nodes remain eligible.
	nodes[1].MonitoringReady = false
	d.selectMonitoring(nodes, nodes[0].At)
	if d.preferredMonitor != "deskb1x" {
		t.Fatal("owner penalty blocked the healthy fallback")
	}
}

func TestMonitoringCountsOnlyFreshHomeAssistantOwnership(t *testing.T) {
	now := time.Now()
	for _, state := range []string{"starting", "active", "checkpointing", "standby"} {
		t.Run(state, func(t *testing.T) {
			a := readyMonitor("deskb1x", 20, now)
			b := readyMonitor("deskb2x", 30, now)
			a.HomeAssistant = &HomeAssistantStatus{State: state, Sync: "synced", At: now, CheckpointAt: now}
			d := &Dashboard{}
			d.selectMonitoring([]Node{a, b}, now)
			expected := b.ID
			if state == "standby" {
				expected = a.ID
			}
			if d.preferredMonitor != expected {
				t.Fatalf("unexpected preference for HA state %s: %s", state, d.preferredMonitor)
			}
			a.HomeAssistant.At = now.Add(-15 * time.Second)
			d = &Dashboard{}
			d.selectMonitoring([]Node{a, b}, now)
			if d.preferredMonitor != a.ID {
				t.Fatal("stale Home Assistant ownership affected preference")
			}
		})
	}
}

func TestMonitoringChargesEachAdditionalOwner(t *testing.T) {
	now := time.Now()
	a := readyMonitor("deskb1x", 20, now)
	b := readyMonitor("deskb2x", 90, now)
	yes := true
	a.VIP["xiaozhi"], a.VIP["dns"], a.VIP["vpn"] = &yes, &yes, &yes
	b.VIP["dns"], b.VIP["vpn"] = &yes, &yes
	d := &Dashboard{}
	d.selectMonitoring([]Node{a, b}, now)
	if d.preferredMonitor != b.ID {
		t.Fatal("three owners incorrectly beat two owners despite the extra stacking cost")
	}
}
func TestMonitorWeightsDoNotChangeHealthOrBlockFallback(t *testing.T) {
	now := time.Now()
	d := &Dashboard{preferredMonitor: "deskb1x", snapshot: Snapshot{At: now}}
	if d.agentWeight("deskb1x", now) != "100%\n" || d.agentWeight("deskb2x", now) != "10%\n" {
		t.Fatal("Unexpected weights")
	}
	for _, node := range []string{"deskb1x", "deskb2x"} {
		if strings.Contains(d.agentWeight(node, now), "down") {
			t.Fatal("Preference modified health")
		}
	}
	if d.agentWeight("deskb2x", now.Add(time.Minute)) != "100%\n" {
		t.Fatal("Stale weights not neutral")
	}
}

func TestNewPlacementFieldsPreserveTelemetryReadiness(t *testing.T) {
	now := time.Now()
	snapshot, members := panelFixture(now)
	n := snapshot.Nodes[0]
	n.MonitoringReady = true
	n.MonitoringPreferred = "deskb3x"
	n.ServiceRoles = map[string]string{"network": "trial"}
	if !validateNode(n, members[0], now) {
		t.Fatal("Placement metadata rejected valid node")
	}
	n.ServiceRoles["network"] = "invented"
	if validateNode(n, members[0], now) {
		t.Fatal("Unvalidated service role accepted")
	}
}

func TestNetworkMetadataIsFreshBoundedAndOptional(t *testing.T) {
	now := time.Now()
	meta := map[string]any{"protocol": "desk-network-v1", "node": "deskb1x", "state": "standby", "sync": "synced", "observed_at": float64(now.Unix()), "subnet": "192.168.1.0/24", "scan_minutes": float64(5), "voter_ready": true, "revision": []any{float64(17), "deskb3x", float64(5)}}
	n := networkMetadata(meta, "deskb1x", now)
	if n == nil || n.Revision.Node != "deskb3x" || n.Revision.Sequence != 5 || n.State != "standby" {
		t.Fatal("Replica metadata lost")
	}

	meta["scan_minutes"] = float64(1)
	meta["scan_schedule"] = "*/1 * * * *"
	changed := networkMetadata(meta, "deskb1x", now)
	if changed == nil || changed.ScanMinutes != 1 || changed.ScanSchedule != "*/1 * * * *" {
		t.Fatal("User scan schedule was discarded")
	}
	meta["scan_minutes"] = float64(0)
	meta["scan_schedule"] = "0 3 * * *"
	if networkMetadata(meta, "deskb1x", now) == nil {
		t.Fatal("Nonuniform cron lost Network metadata")
	}
	meta["scan_minutes"] = float64(61)
	if networkMetadata(meta, "deskb1x", now) != nil {
		t.Fatal("Unbounded scan interval accepted")
	}
	meta["scan_minutes"] = float64(5)
	delete(meta, "scan_schedule")
	snapshot, members := panelFixture(now)
	node := snapshot.Nodes[0]
	node.Network = n
	if !validateNode(node, members[0], now) {
		t.Fatal("Network metadata rejected healthy node")
	}
	if networkMetadata(meta, "deskb2x", now) != nil || networkMetadata(meta, "deskb1x", now.Add(16*time.Second)) != nil {
		t.Fatal("Wrong identity or stale metadata accepted")
	}
	meta["revision"] = []any{float64(17), "unknown", float64(5)}
	if networkMetadata(meta, "deskb1x", now) != nil {
		t.Fatal("Invalid revision member accepted")
	}
	meta["revision"] = []any{float64(17), "deskb3x", float64(5.5)}
	if networkMetadata(meta, "deskb1x", now) != nil {
		t.Fatal("Fractional checkpoint accepted")
	}
	if !validNetwork(nil) {
		t.Fatal("Missing Network must preserve other telemetry")
	}
}
