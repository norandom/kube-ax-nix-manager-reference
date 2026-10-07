package main

import (
	"context"
	"io"
	"log/slog"
	"os"
	"testing"

	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
)

const engineJSON = `{"rules": [{"tlsPassthrough": {"hostnames": ["files.because-security.com"], "ports": {"numbers": [443]}}}]}`

type fakeAPI struct {
	ateapipb.ControlClient // unimplemented methods panic
	actors                 []string
	policies               map[string]*ateapipb.EgressPolicy
	creates                []string
	failGet                bool
}

func (f *fakeAPI) ListActors(_ context.Context, r *ateapipb.ListActorsRequest, _ ...grpc.CallOption) (*ateapipb.ListActorsResponse, error) {
	resp := &ateapipb.ListActorsResponse{}
	for _, n := range f.actors {
		resp.Actors = append(resp.Actors, &ateapipb.Actor{Metadata: &ateapipb.ResourceMetadata{Atespace: r.GetAtespace(), Name: n}})
	}
	return resp, nil
}

func (f *fakeAPI) GetActorEgressPolicy(_ context.Context, r *ateapipb.GetActorEgressPolicyRequest, _ ...grpc.CallOption) (*ateapipb.EgressPolicy, error) {
	if f.failGet {
		return nil, status.Error(codes.Unavailable, "down")
	}
	if p, ok := f.policies[r.GetActor().GetName()]; ok {
		return p, nil
	}
	return nil, status.Error(codes.NotFound, "none")
}

func (f *fakeAPI) CreateActorEgressPolicy(_ context.Context, r *ateapipb.CreateActorEgressPolicyRequest, _ ...grpc.CallOption) (*ateapipb.EgressPolicy, error) {
	f.creates = append(f.creates, r.GetActor().GetName())
	f.policies[r.GetActor().GetName()] = r.GetEgressPolicy()
	return r.GetEgressPolicy(), nil
}

func policy(t *testing.T) *ateapipb.EgressPolicy {
	var p ateapipb.EgressPolicy
	if err := protojson.Unmarshal([]byte(engineJSON), &p); err != nil {
		t.Fatalf("engine policy JSON rejected: %v", err)
	}
	return &p
}

func routesOf(t *testing.T, name string) []route {
	raw, err := os.ReadFile("../../platform/substrate/egress-routes/" + name + ".json")
	if err != nil {
		t.Fatal(err)
	}
	r, err := parseRoutes(raw)
	if err != nil {
		t.Fatalf("%s.json: %v", name, err)
	}
	return r
}

func one(t *testing.T) []route { return []route{{prefix: "ousast-engine-", policy: policy(t)}} }

func hosts(p *ateapipb.EgressPolicy) []string {
	if p == nil {
		return nil
	}
	return p.GetRules()[0].GetTlsPassthrough().GetHostnames()
}

// The committed route files, checked against what OpenUltraSAST asked for on 2026-10-05.
func TestRouteFiles(t *testing.T) {
	open, scoped := routesOf(t, "open"), routesOf(t, "scoped")
	cases := []struct {
		name         string
		open, scoped string // "" = no egress
	}{
		{"ousast-engine-search-exec-1", "*", "*"},
		{"ousast-engine-search-verify-1", "", "files.because-security.com"},
		{"ousast-engine-search-probe-1", "*", "files.because-security.com"},
		{"ousast-engine-pass-1", "*", "files.because-security.com"},
		{"ousast-engine-replay-1", "*", "files.because-security.com,github.com"},
		{"smoke-1", "*", "*"},
		// OpenUltraSAST 2026-10-06: deny by default inside ousast-engine-* (scoped only).
		{"ousast-engine-pin-search-verify-1", "*", ""},
		{"ousast-engine-other-1", "*", ""},
		{"egtest-files-1", "files.because-security.com", "files.because-security.com"},
		{"egtest-replay-1", "files.because-security.com,github.com", "files.because-security.com,github.com"},
	}
	join := func(h []string) string {
		out := ""
		for i, x := range h {
			if i > 0 {
				out += ","
			}
			out += x
		}
		return out
	}
	for _, c := range cases {
		if got := join(hosts(policyFor(c.name, open))); got != c.open {
			t.Errorf("open: %s -> %q, want %q", c.name, got, c.open)
		}
		if got := join(hosts(policyFor(c.name, scoped))); got != c.scoped {
			t.Errorf("scoped: %s -> %q, want %q", c.name, got, c.scoped)
		}
	}
}

func TestParseRoutesRejectsMistakes(t *testing.T) {
	bad := map[string]string{
		"unknown field":  `{"routes":[{"prefx":"a","policy":null}]}`,
		"duplicate":      `{"routes":[{"prefix":"a","policy":null},{"prefix":"a","policy":null}]}`,
		"empty policy":   `{"routes":[{"prefix":"a","policy":{}}]}`,
		"typo in policy": `{"routes":[{"prefix":"a","policy":{"rules":[{"tlsPasthrough":{}}]}}]}`,
		"no routes":      `{"routes":[]}`,
	}
	for what, raw := range bad {
		if _, err := parseRoutes([]byte(raw)); err == nil {
			t.Errorf("%s: accepted", what)
		}
	}
}

func TestPolicyJSONMatchesProto(t *testing.T) {
	p := policy(t)
	tp := p.GetRules()[0].GetTlsPassthrough()
	if tp.GetHostnames()[0] != "files.because-security.com" || tp.GetPorts().GetNumbers()[0] != 443 {
		t.Fatalf("unexpected parse: %v", p)
	}
	var bad ateapipb.EgressPolicy
	if protojson.Unmarshal([]byte(`{"rules":[{"tlsPasthrough":{}}]}`), &bad) == nil {
		t.Fatal("typo in policy must be rejected")
	}
}

func TestAttachesOnlyPrefixedAndNeverOverwrites(t *testing.T) {
	existing := &ateapipb.EgressPolicy{}
	api := &fakeAPI{
		actors:   []string{"ousast-engine-1", "ousast-engine-2", "ousast-other", "smoke-1"},
		policies: map[string]*ateapipb.EgressPolicy{"ousast-engine-2": existing},
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	attached := map[string]bool{}
	if _, err := reconcile(context.Background(), api, log, "default", one(t), attached); err != nil {
		t.Fatal(err)
	}
	if len(api.creates) != 1 || api.creates[0] != "ousast-engine-1" {
		t.Fatalf("creates = %v, want only ousast-engine-1", api.creates)
	}
	if api.policies["ousast-engine-2"] != existing {
		t.Fatal("existing policy was overwritten")
	}
	got := api.policies["ousast-engine-1"]
	if got.GetMetadata().GetName() != "default" || got.GetMetadata().GetAtespace() != "default" {
		t.Fatalf("metadata = %v", got.GetMetadata())
	}
	// Second poll: nothing new, no extra calls.
	if _, err := reconcile(context.Background(), api, log, "default", one(t), attached); err != nil {
		t.Fatal(err)
	}
	if len(api.creates) != 1 {
		t.Fatalf("re-created on second poll: %v", api.creates)
	}
}

func TestRetriesAfterFailure(t *testing.T) {
	api := &fakeAPI{actors: []string{"ousast-engine-9"}, policies: map[string]*ateapipb.EgressPolicy{}, failGet: true}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	attached := map[string]bool{}
	_, _ = reconcile(context.Background(), api, log, "default", one(t), attached)
	if attached["ousast-engine-9"] {
		t.Fatal("marked attached despite failure")
	}
	api.failGet = false
	_, _ = reconcile(context.Background(), api, log, "default", one(t), attached)
	if !attached["ousast-engine-9"] || len(api.creates) != 1 {
		t.Fatalf("not retried: attached=%v creates=%v", attached, api.creates)
	}
}

func TestNullRouteGetsNoPolicy(t *testing.T) {
	api := &fakeAPI{
		actors:   []string{"ousast-engine-search-exec-1", "ousast-engine-search-verify-1", "ousast-engine-x"},
		policies: map[string]*ateapipb.EgressPolicy{},
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	if _, err := reconcile(context.Background(), api, log, "default", routesOf(t, "open"), map[string]bool{}); err != nil {
		t.Fatal(err)
	}
	if _, ok := api.policies["ousast-engine-search-verify-1"]; ok {
		t.Fatal("verifier actor got a policy")
	}
	if len(api.creates) != 2 {
		t.Fatalf("creates = %v, want exec-1 and x", api.creates)
	}
}
