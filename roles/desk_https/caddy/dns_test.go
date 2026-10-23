package proxyv2

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	cloudflare "github.com/libdns/cloudflare"
	"github.com/libdns/libdns"
)

// Cloudflare mock deliberately returns overlapping TXT values for a contains
// query. The pinned provider must remove only its own exact challenge value.
type dnsAPI struct {
	mu      sync.Mutex
	records map[string]map[string]any
	next    int
}

func (a *dnsAPI) Do(req *http.Request) (*http.Response, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	var result any
	switch {
	case req.Method == "GET" && strings.HasSuffix(req.URL.Path, "/zones"):
		result = []map[string]any{{"id": "zone", "name": "huynm.dev"}}
	case req.Method == "POST":
		var row map[string]any
		if err := json.NewDecoder(req.Body).Decode(&row); err != nil {
			return nil, err
		}
		a.next++
		id := fmt.Sprint(a.next)
		row["id"] = id
		a.records[id] = row
		result = row
	case req.Method == "GET":
		rows := []map[string]any{}
		for _, row := range a.records {
			rows = append(rows, row)
		}
		result = rows
	case req.Method == "DELETE":
		id := req.URL.Path[strings.LastIndex(req.URL.Path, "/")+1:]
		result = a.records[id]
		delete(a.records, id)
	default:
		return nil, fmt.Errorf("unexpected API method: %s", req.Method)
	}
	data, err := json.Marshal(map[string]any{"success": true, "result": result, "errors": []any{}})
	return &http.Response{StatusCode: 200, Header: http.Header{}, Body: io.NopCloser(bytes.NewReader(data))}, err
}

func TestIndependentNodesKeepConcurrentDNSChallenges(t *testing.T) {
	api := &dnsAPI{records: map[string]map[string]any{"unrelated": {"id": "unrelated", "type": "TXT", "name": "_acme-challenge.cluster.huynm.dev", "content": "challenge-a-suffix", "ttl": 120}}}
	values := []string{"challenge-a", "challenge-b", "challenge-c"}
	errCh := make(chan error, 3)
	var wg sync.WaitGroup
	for _, value := range values {
		wg.Add(1)
		go func(value string) {
			defer wg.Done()
			p := cloudflare.Provider{APIToken: "fixture", HTTPClient: api}
			_, err := p.AppendRecords(context.Background(), "huynm.dev.", []libdns.Record{libdns.TXT{Name: "_acme-challenge.cluster", Text: value, TTL: 120 * time.Second}})
			errCh <- err
		}(value)
	}
	wg.Wait()
	close(errCh)
	for err := range errCh {
		if err != nil {
			t.Fatal(err)
		}
	}
	if len(api.records) != 4 {
		t.Fatalf("concurrent challenges replaced: %v", api.records)
	}
	// A fresh provider models renewal cleanup after a process restart.
	for i, value := range values {
		p := cloudflare.Provider{APIToken: "fixture", HTTPClient: api}
		if _, err := p.DeleteRecords(context.Background(), "huynm.dev.", []libdns.Record{libdns.TXT{Name: "_acme-challenge.cluster", Text: value}}); err != nil {
			t.Fatal(err)
		}
		if len(api.records) != 3-i {
			t.Fatalf("cleanup removed another node's TXT: %v", api.records)
		}
	}
	if api.records["unrelated"] == nil {
		t.Fatal("unrelated TXT was deleted")
	}
}
