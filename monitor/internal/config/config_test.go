package config

import (
	"os"
	"strings"
	"testing"
	"time"
)

const minimal = `
deployment:
  kind: self-hosted
  namespace: default
  address: temporal:7233
defaults:
  poll_interval: 60s
  window_poll_interval: 600s
  buckets: [1, 2, 5]
  slo_window: 28d
workflow_types:
  - name: OrderWorkflow
    task_queue: orders
    budget: 4h
    objective: 0.99
    owner: team-orders
`

func TestParse_DurationStrings(t *testing.T) {
	// The generated config writes Go-style duration strings ("60s", "4h",
	// "2.7s", "28d"). If these do not round-trip, every downstream step is
	// reading zero-valued durations.
	c, err := Parse([]byte(minimal))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if c.Defaults.PollInterval.Std() != 60*time.Second {
		t.Errorf("poll_interval = %v, want 60s", c.Defaults.PollInterval.Std())
	}
	if c.Defaults.SLOWindow.Std() != 28*24*time.Hour {
		t.Errorf("slo_window = %v, want 672h (28d)", c.Defaults.SLOWindow.Std())
	}
	if c.WorkflowTypes[0].Budget.Std() != 4*time.Hour {
		t.Errorf("budget = %v, want 4h", c.WorkflowTypes[0].Budget.Std())
	}
}

func TestParse_GeneratedConfigRoundTrips(t *testing.T) {
	// budget-derive emits this shape. If it does not parse, step 1 hands step 2
	// something unusable.
	path := "../../slo-config.generated.yaml"
	data, err := os.ReadFile(path)
	if err != nil {
		t.Skipf("no generated config at %s (run budget-derive first)", path)
	}
	c, err := Parse(data)
	if err != nil {
		t.Fatalf("generated config does not parse: %v", err)
	}
	if len(c.WorkflowTypes) == 0 {
		t.Fatal("generated config parsed but has no workflow types")
	}
	for _, w := range c.WorkflowTypes {
		if w.Budget.Std() <= 0 {
			t.Errorf("%s: budget parsed as %v — duration string not decoded", w.Name, w.Budget.Std())
		}
	}
}

func TestValidate_PollFloor(t *testing.T) {
	// Visibility is rate-limited; a tight loop across many types is the fastest
	// way to get throttled.
	cfg := strings.Replace(minimal, "poll_interval: 60s", "poll_interval: 5s", 1)
	if _, err := Parse([]byte(cfg)); err == nil {
		t.Fatal("want error for poll_interval below the floor")
	} else if !strings.Contains(err.Error(), "rate-limited") {
		t.Errorf("error should explain why: %v", err)
	}
}

func TestValidate_BucketsMustIncludeOne(t *testing.T) {
	// bucket="1" is the still-running-past-budget term in the SLI denominator.
	// Without it the SLO silently ignores open violations, which is the exact
	// failure this whole design exists to prevent.
	cfg := strings.Replace(minimal, "buckets: [1, 2, 5]", "buckets: [2, 5]", 1)
	_, err := Parse([]byte(cfg))
	if err == nil {
		t.Fatal("want error when bucket 1 is missing")
	}
	if !strings.Contains(err.Error(), "denominator") {
		t.Errorf("error should say why bucket 1 matters: %v", err)
	}
}

func TestValidate_WindowPollNotFasterThanFast(t *testing.T) {
	cfg := strings.Replace(minimal, "window_poll_interval: 600s", "window_poll_interval: 45s", 1)
	if _, err := Parse([]byte(cfg)); err == nil {
		t.Fatal("want error when the expensive loop polls faster than the cheap one")
	}
}

func TestValidate_OwnerRequired(t *testing.T) {
	cfg := strings.Replace(minimal, "    owner: team-orders", "", 1)
	if _, err := Parse([]byte(cfg)); err == nil {
		t.Fatal("want error for missing owner")
	}
}

func TestValidate_ObjectiveBounds(t *testing.T) {
	for _, bad := range []string{"1.0", "0", "1.5"} {
		cfg := strings.Replace(minimal, "objective: 0.99", "objective: "+bad, 1)
		if _, err := Parse([]byte(cfg)); err == nil {
			t.Errorf("objective %s should be rejected", bad)
		}
	}
}

func TestValidate_DuplicateWorkflowType(t *testing.T) {
	cfg := minimal + `
  - name: OrderWorkflow
    budget: 1h
    objective: 0.99
    owner: team-other
`
	if _, err := Parse([]byte(cfg)); err == nil {
		t.Fatal("want error for duplicate workflow type name")
	}
}

func TestDefaults_ClosedStatusesIncludeTerminated(t *testing.T) {
	c, err := Parse([]byte(minimal))
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, s := range c.Defaults.ClosedOverBudgetStatuses {
		if s == "Terminated" {
			found = true
		}
	}
	if !found {
		t.Error("Terminated must default into the closed set, or terminating a late workflow improves the SLO")
	}
}

func TestParseDuration_DaysAndWeeks(t *testing.T) {
	// Go's time.ParseDuration has no day unit — "28d" is a parse error, not 28
	// days. The config contract uses 28d, so this translation is required for
	// the service to start at all.
	cases := map[string]time.Duration{
		"28d":   28 * 24 * time.Hour,
		"1d":    24 * time.Hour,
		"2w":    14 * 24 * time.Hour,
		"1.5d":  36 * time.Hour,
		"90s":   90 * time.Second,
		"4h30m": 4*time.Hour + 30*time.Minute,
		"500ms": 500 * time.Millisecond,
	}
	for in, want := range cases {
		got, err := ParseDuration(in)
		if err != nil {
			t.Errorf("%s: %v", in, err)
			continue
		}
		if got != want {
			t.Errorf("%s = %v, want %v", in, got, want)
		}
	}
	if _, err := ParseDuration("28 days"); err == nil {
		t.Error("garbage should be rejected")
	}
}

func TestParse_BareNumberIsSeconds(t *testing.T) {
	// A bare `poll_interval: 60` must mean 60 seconds. Decoded as a raw
	// time.Duration it would be 60 NANOSECONDS — a silent 10^9x error that
	// would hammer the Visibility API.
	cfg := strings.Replace(minimal, "poll_interval: 60s", "poll_interval: 60", 1)
	c, err := Parse([]byte(cfg))
	if err != nil {
		t.Fatal(err)
	}
	if c.Defaults.PollInterval.Std() != 60*time.Second {
		t.Errorf("bare 60 = %v, want 60s", c.Defaults.PollInterval.Std())
	}
}
