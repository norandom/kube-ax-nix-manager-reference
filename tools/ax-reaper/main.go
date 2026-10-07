// ax-reaper deletes AX tasks whose command exited more than --grace ago (safety net if a dispatcher
// dies before cleaning up). AX v0.3.1 never marks tasks finished: after the command exits the task
// stays "Running" and keeps its worker, which with one task per worker blocks capacity.
//
// Exit evidence comes from the AX task runner's own log line ("task command completed successfully" /
// "task command exited with error"), which Substrate writes into the worker pod log as JSON labelled
// with ate.actor.name (AX names the actor after the task). Only exits observed AFTER the task's
// creation count, so a re-created task with the same name is never deleted for an old run.
package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	axv1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

const saDir = "/var/run/secrets/kubernetes.io/serviceaccount"

var exitMarkers = []string{"task command completed successfully", "task command exited with error"}

type logLine struct {
	Labels  map[string]string `json:"labels"`
	Message string            `json:"message"`
}

// exits maps actor name -> latest exit time seen in the worker logs.
func exitsFromLogs(r *bufio.Scanner, prefix string, into map[string]time.Time) {
	for r.Scan() {
		line := r.Text()
		ts, rest, ok := strings.Cut(line, " ") // kubelet prefixes RFC3339Nano timestamps
		if !ok || !strings.HasPrefix(rest, `{"labels"`) {
			continue
		}
		var l logLine
		if json.Unmarshal([]byte(rest), &l) != nil {
			continue
		}
		name := l.Labels["ate.actor.name"]
		if !strings.HasPrefix(name, prefix) || !isExit(l.Message) {
			continue
		}
		t, err := time.Parse(time.RFC3339Nano, ts)
		if err != nil {
			continue
		}
		if t.After(into[name]) {
			into[name] = t
		}
	}
}

func isExit(msg string) bool {
	for _, m := range exitMarkers {
		if strings.Contains(msg, m) {
			return true
		}
	}
	return false
}

// due returns the tasks to delete: prefix match, an exit observed after creation, older than grace.
func due(tasks []*axv1.Task, exits map[string]time.Time, prefix string, grace time.Duration, now time.Time) []*axv1.Task {
	var out []*axv1.Task
	for _, t := range tasks {
		name := t.GetMetadata().GetName()
		if !strings.HasPrefix(name, prefix) {
			continue
		}
		exit, ok := exits[name]
		created := t.GetMetadata().GetCreationTimestamp().AsTime()
		if !ok || !exit.After(created) || now.Sub(exit) < grace {
			continue
		}
		out = append(out, t)
	}
	return out
}

type kube struct {
	client *http.Client
	host   string
	token  string
}

func newKube() (*kube, error) {
	ca, err := os.ReadFile(saDir + "/ca.crt")
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(ca)
	tok, err := os.ReadFile(saDir + "/token")
	if err != nil {
		return nil, err
	}
	host := "https://" + os.Getenv("KUBERNETES_SERVICE_HOST") + ":" + os.Getenv("KUBERNETES_SERVICE_PORT")
	return &kube{
		client: &http.Client{Timeout: 60 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}}},
		host:   host, token: strings.TrimSpace(string(tok)),
	}, nil
}

func (k *kube) get(ctx context.Context, path string, q url.Values) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, k.host+path+"?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+k.token)
	resp, err := k.client.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("GET %s: %s", path, resp.Status)
	}
	return resp, nil
}

func (k *kube) workerExits(ctx context.Context, ns, prefix string, window time.Duration) (map[string]time.Time, error) {
	resp, err := k.get(ctx, "/api/v1/namespaces/"+ns+"/pods", url.Values{})
	if err != nil {
		return nil, err
	}
	var pods struct {
		Items []struct {
			Metadata struct{ Name string }  `json:"metadata"`
			Status   struct{ Phase string } `json:"status"`
		} `json:"items"`
	}
	err = json.NewDecoder(resp.Body).Decode(&pods)
	resp.Body.Close()
	if err != nil {
		return nil, err
	}
	exits := map[string]time.Time{}
	for _, p := range pods.Items {
		if p.Status.Phase != "Running" {
			continue
		}
		q := url.Values{"timestamps": {"true"}, "sinceSeconds": {fmt.Sprint(int(window.Seconds()))}}
		r, err := k.get(ctx, "/api/v1/namespaces/"+ns+"/pods/"+p.Metadata.Name+"/log", q)
		if err != nil {
			return nil, err
		}
		sc := bufio.NewScanner(r.Body)
		sc.Buffer(make([]byte, 1<<20), 4<<20)
		exitsFromLogs(sc, prefix, exits)
		r.Body.Close()
	}
	return exits, nil
}

func main() {
	server := flag.String("ax-server", "ax-server.ax-system.svc.cluster.local:8080", "AX API (gRPC, no auth in v0.3.1)")
	atespace := flag.String("atespace", "default", "atespace to watch")
	prefix := flag.String("prefix", "ousast-engine-", "only tasks with this name prefix")
	grace := flag.Duration("grace", 10*time.Minute, "delete this long after the command exited")
	interval := flag.Duration("interval", time.Minute, "poll interval")
	window := flag.Duration("log-window", 2*time.Hour, "how far back to read worker logs")
	workerNS := flag.String("worker-namespace", "ax-workers", "namespace of the Substrate worker pods")
	dryRun := flag.Bool("dry-run", false, "log what would be deleted, delete nothing")
	flag.Parse()
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	k, err := newKube()
	if err != nil {
		log.Error("kubernetes client", "err", err)
		os.Exit(1)
	}
	conn, err := grpc.NewClient(*server, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Error("dial ax-server", "err", err)
		os.Exit(1)
	}
	defer conn.Close()
	ax := axv1.NewAXClient(conn)
	log.Info("reaping", "prefix", *prefix, "grace", grace.String(), "dryRun", *dryRun)

	for {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
		if err := once(ctx, log, k, ax, *atespace, *prefix, *workerNS, *grace, *window, *dryRun); err != nil {
			log.Warn("poll failed", "err", err)
		}
		cancel()
		time.Sleep(*interval)
	}
}

func once(ctx context.Context, log *slog.Logger, k *kube, ax axv1.AXClient, atespace, prefix, workerNS string,
	grace, window time.Duration, dryRun bool) error {
	resp, err := ax.ListTasks(ctx, &axv1.ListTasksRequest{Atespace: atespace})
	if err != nil {
		return fmt.Errorf("ListTasks: %w", err)
	}
	exits, err := k.workerExits(ctx, workerNS, prefix, window)
	if err != nil {
		return fmt.Errorf("worker logs: %w", err)
	}
	for _, t := range due(resp.GetTasks(), exits, prefix, grace, time.Now()) {
		name := t.GetMetadata().GetName()
		if dryRun {
			log.Info("would delete", "task", name, "exited", exits[name])
			continue
		}
		if _, err := ax.DeleteTask(ctx, &axv1.DeleteTaskRequest{Atespace: atespace, Name: name}); err != nil {
			log.Warn("delete failed", "task", name, "err", err)
			continue
		}
		log.Info("deleted finished task", "task", name, "exited", exits[name])
	}
	return nil
}
