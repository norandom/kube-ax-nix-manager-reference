package main

import (
	"bufio"
	"strings"
	"testing"
	"time"

	axv1 "github.com/google/ax/pkg/apis/v1alpha1"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const logs = `2026-10-05T10:00:00.000000000Z {"labels":{"ate.actor.name":"ousast-engine-a"},"message":"time=x level=INFO msg=\"started task command\""}
2026-10-05T10:05:00.000000000Z {"labels":{"ate.actor.name":"ousast-engine-a"},"message":"time=x level=INFO msg=\"task command completed successfully\" pid=9"}
2026-10-05T10:06:00.000000000Z {"labels":{"ate.actor.name":"ousast-engine-b"},"message":"time=x level=ERROR msg=\"task command exited with error\" exitCode=1"}
2026-10-05T10:07:00.000000000Z {"labels":{"ate.actor.name":"other-c"},"message":"msg=\"task command completed successfully\""}
2026-10-05T10:08:00.000000000Z {"time":"x","level":"INFO","msg":"Handle RPC"}
`

func task(name string, created time.Time) *axv1.Task {
	return &axv1.Task{Metadata: &axv1.ObjectMeta{Name: name, Atespace: "default", CreationTimestamp: timestamppb.New(created)}}
}

func TestExitsAndDue(t *testing.T) {
	exits := map[string]time.Time{}
	exitsFromLogs(bufio.NewScanner(strings.NewReader(logs)), "ousast-engine-", exits)
	if len(exits) != 2 {
		t.Fatalf("exits = %v, want a and b only", exits)
	}
	base := time.Date(2026, 10, 5, 9, 59, 0, 0, time.UTC)
	tasks := []*axv1.Task{
		task("ousast-engine-a", base),  // exited 10:05
		task("ousast-engine-b", base),  // exited 10:06
		task("ousast-engine-d", base),  // never exited
		task("other-c", base),          // wrong prefix
		task("ousast-engine-a2", base), // no log
	}
	// 10:15:30: a exited 10m30s ago -> due; b exited 9m30s ago -> not yet.
	got := due(tasks, exits, "ousast-engine-", 10*time.Minute, time.Date(2026, 10, 5, 10, 15, 30, 0, time.UTC))
	if len(got) != 1 || got[0].GetMetadata().GetName() != "ousast-engine-a" {
		t.Fatalf("due = %v", got)
	}
	// Re-created after its old exit: never due for the old run.
	recreated := []*axv1.Task{task("ousast-engine-a", time.Date(2026, 10, 5, 10, 10, 0, 0, time.UTC))}
	if got := due(recreated, exits, "ousast-engine-", 10*time.Minute, time.Date(2026, 10, 5, 12, 0, 0, 0, time.UTC)); len(got) != 0 {
		t.Fatalf("re-created task was due: %v", got)
	}
}
