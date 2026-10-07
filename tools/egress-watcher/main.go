// egress-watcher attaches an EgressPolicy to every new Agent Substrate actor, chosen by actor name:
// a routes file maps name prefixes to policies, the longest matching prefix wins, and a route with
// no policy (or no matching route) means the actor gets none, i.e. no egress at all.
//
// Substrate v0.3.0 has per-actor egress policies only and no API authorization, so the
// full-access identity stays here, operator-side, doing exactly this one job. It is create-only:
// existing policies are never changed or deleted.
package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
)

type tokenAuth struct{ file string }

func (t tokenAuth) GetRequestMetadata(context.Context, ...string) (map[string]string, error) {
	b, err := os.ReadFile(t.file) // re-read every call: the projected token rotates
	if err != nil {
		return nil, err
	}
	return map[string]string{"authorization": "Bearer " + strings.TrimSpace(string(b))}, nil
}
func (tokenAuth) RequireTransportSecurity() bool { return true }

func main() {
	endpoint := flag.String("endpoint", "api.ate-system.svc.cluster.local:443", "Substrate API")
	authority := flag.String("authority", "api.ate-system.svc", "TLS server name / :authority")
	tokenFile := flag.String("token-file", "/var/run/secrets/ateapi/token", "bearer token (audience api.ate-system.svc)")
	caFile := flag.String("ca-file", "/run/servicedns-ca/trust-bundle.pem", "Substrate service CA bundle")
	atespace := flag.String("atespace", "default", "atespace to watch")
	routesFile := flag.String("routes-file", "/etc/egress-watcher/routes.json",
		`{"routes":[{"prefix":"...","policy":<EgressPolicy protojson>|null}]}; longest prefix wins`)
	interval := flag.Duration("interval", time.Second, "poll interval")
	healthAddr := flag.String("health-addr", ":8080", "health endpoint")
	flag.Parse()
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	raw, err := os.ReadFile(*routesFile)
	if err != nil {
		log.Error("reading routes", "err", err)
		os.Exit(1)
	}
	routes, err := parseRoutes(raw)
	if err != nil {
		log.Error("invalid routes", "err", err)
		os.Exit(1)
	}

	ca, err := os.ReadFile(*caFile)
	if err != nil {
		log.Error("reading CA", "err", err)
		os.Exit(1)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(ca) {
		log.Error("no certificates in CA file")
		os.Exit(1)
	}
	conn, err := grpc.NewClient(*endpoint,
		grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{RootCAs: pool, ServerName: *authority, MinVersion: tls.VersionTLS12})),
		grpc.WithPerRPCCredentials(tokenAuth{*tokenFile}),
		grpc.WithAuthority(*authority))
	if err != nil {
		log.Error("dial", "err", err)
		os.Exit(1)
	}
	defer conn.Close()
	api := ateapipb.NewControlClient(conn)

	var lastOK atomic.Int64
	go func() {
		http.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
			if time.Since(time.Unix(lastOK.Load(), 0)) > 30*time.Second {
				http.Error(w, "no successful poll in 30s", http.StatusServiceUnavailable)
				return
			}
			fmt.Fprintln(w, "ok")
		})
		_ = http.ListenAndServe(*healthAddr, nil)
	}()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	for _, r := range routes {
		log.Info("route", "prefix", r.prefix, "egress", r.policy != nil, "rules", len(r.policy.GetRules()))
	}
	log.Info("watching", "atespace", *atespace)

	attached := map[string]bool{} // actor name -> policy known to exist
	tick := time.NewTicker(*interval)
	defer tick.Stop()
	for {
		seen, err := reconcile(ctx, api, log, *atespace, routes, attached)
		if err != nil {
			log.Warn("poll failed", "err", err)
		} else {
			lastOK.Store(time.Now().Unix())
			for name := range attached { // forget deleted actors so memory stays bounded
				if !seen[name] {
					delete(attached, name)
				}
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}

type route struct {
	prefix string
	policy *ateapipb.EgressPolicy // nil: no policy, no egress
}

// parseRoutes reads the routes file, strictly: unknown fields, duplicate prefixes and empty
// policies fail, so a typo can never silently widen or drop egress.
func parseRoutes(raw []byte) ([]route, error) {
	var file struct {
		Routes []struct {
			Prefix string          `json:"prefix"`
			Policy json.RawMessage `json:"policy"`
		} `json:"routes"`
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&file); err != nil {
		return nil, err
	}
	if len(file.Routes) == 0 {
		return nil, fmt.Errorf("no routes")
	}
	seen := map[string]bool{}
	var out []route
	for _, r := range file.Routes {
		if seen[r.Prefix] {
			return nil, fmt.Errorf("duplicate prefix %q", r.Prefix)
		}
		seen[r.Prefix] = true
		rt := route{prefix: r.Prefix}
		if len(r.Policy) > 0 && string(r.Policy) != "null" {
			var p ateapipb.EgressPolicy
			if err := (protojson.UnmarshalOptions{}).Unmarshal(r.Policy, &p); err != nil {
				return nil, fmt.Errorf("prefix %q: %w", r.Prefix, err)
			}
			if len(p.GetRules()) == 0 {
				return nil, fmt.Errorf("prefix %q: policy has no rules (use null for no egress)", r.Prefix)
			}
			rt.policy = &p
		}
		out = append(out, rt)
	}
	sort.Slice(out, func(i, j int) bool { return len(out[i].prefix) > len(out[j].prefix) })
	return out, nil
}

// policyFor returns the policy of the longest matching prefix (nil: no egress).
func policyFor(name string, routes []route) *ateapipb.EgressPolicy {
	for _, r := range routes { // sorted longest first
		if strings.HasPrefix(name, r.prefix) {
			return r.policy
		}
	}
	return nil
}

func reconcile(ctx context.Context, api ateapipb.ControlClient, log *slog.Logger, atespace string,
	routes []route, attached map[string]bool) (map[string]bool, error) {
	seen := map[string]bool{}
	token := ""
	for {
		cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		resp, err := api.ListActors(cctx, &ateapipb.ListActorsRequest{Atespace: atespace, PageSize: 500, PageToken: token})
		cancel()
		if err != nil {
			return nil, fmt.Errorf("ListActors: %w", err)
		}
		for _, a := range resp.GetActors() {
			name := a.GetMetadata().GetName()
			policy := policyFor(name, routes)
			if policy == nil {
				continue
			}
			seen[name] = true
			if attached[name] {
				continue
			}
			if err := ensurePolicy(ctx, api, atespace, name, policy); err != nil {
				log.Warn("attach failed", "actor", name, "err", err)
				continue
			}
			attached[name] = true
			log.Info("egress policy present", "actor", name)
		}
		if token = resp.GetNextPageToken(); token == "" {
			return seen, nil
		}
	}
}

func ensurePolicy(ctx context.Context, api ateapipb.ControlClient, atespace, actor string, policy *ateapipb.EgressPolicy) error {
	ref := &ateapipb.ObjectRef{Atespace: atespace, Name: actor}
	cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if _, err := api.GetActorEgressPolicy(cctx, &ateapipb.GetActorEgressPolicyRequest{Actor: ref}); err == nil {
		return nil // already has a policy: never overwrite
	} else if status.Code(err) != codes.NotFound {
		return fmt.Errorf("GetActorEgressPolicy: %w", err)
	}
	p := &ateapipb.EgressPolicy{Metadata: &ateapipb.ResourceMetadata{Atespace: atespace, Name: "default"}, Rules: policy.GetRules()}
	_, err := api.CreateActorEgressPolicy(cctx, &ateapipb.CreateActorEgressPolicyRequest{Actor: ref, EgressPolicy: p})
	if status.Code(err) == codes.AlreadyExists {
		return nil
	}
	return err
}
