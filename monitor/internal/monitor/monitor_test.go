package monitor

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"
	"go.temporal.io/api/serviceerror"

	"github.com/temporal-slo-demo/monitor/internal/config"
	"github.com/temporal-slo-demo/monitor/internal/metrics"
)

// fakeCounter answers by matching substrings of the query, so tests express
// intent ("the 2x bucket") rather than reproducing query syntax.
type fakeCounter struct {
	answers map[string]int64
	err     error
	failIf  func(q string) error // per-query failure, for partial-failure tests
	queries []string
}

func (f *fakeCounter) Count(_ context.Context, q string) (int64, error) {
	f.queries = append(f.queries, q)
	if f.err != nil {
		return 0, f.err
	}
	if f.failIf != nil {
		if err := f.failIf(q); err != nil {
			return 0, err
		}
	}
	for frag, n := range f.answers {
		if strings.Contains(q, frag) {
			return n, nil
		}
	}
	return 0, nil
}

func testSetup(t *testing.T, c *fakeCounter) (*Monitor, *metrics.Metrics, *prometheus.Registry) {
	t.Helper()
	reg := prometheus.NewRegistry()
	m := metrics.New(reg)
	cfg := &config.Config{
		Deployment: config.Deployment{
			Kind: config.SelfHosted, Namespace: "default",
			Address: "x:7233", StuckDetection: config.StuckAuto,
		},
		Defaults: config.Defaults{
			PollInterval:             config.Duration(60 * time.Second),
			WindowPollInterval:       config.Duration(600 * time.Second),
			Buckets:                  []int{1, 2, 5},
			SLOWindow:                config.Duration(28 * 24 * time.Hour),
			ClosedOverBudgetStatuses: []string{"Completed", "Failed", "Terminated"},
		},
		WorkflowTypes: []config.WorkflowType{{
			Name: "OrderWorkflow", TaskQueue: "orders",
			Budget: config.Duration(time.Hour), Objective: 0.99, Owner: "payments",
		}},
	}
	mo := New(cfg, c, m, slog.New(slog.NewTextHandler(io.Discard, nil)))
	return mo, m, reg
}

func gaugeValue(t *testing.T, reg *prometheus.Registry, name string, labels map[string]string) (float64, bool) {
	t.Helper()
	fams, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, f := range fams {
		if f.GetName() != name {
			continue
		}
		for _, mm := range f.GetMetric() {
			if !labelsMatch(mm, labels) {
				continue
			}
			if g := mm.GetGauge(); g != nil {
				return g.GetValue(), true
			}
		}
	}
	return 0, false
}

func labelsMatch(m *dto.Metric, want map[string]string) bool {
	got := map[string]string{}
	for _, lp := range m.GetLabel() {
		got[lp.GetName()] = lp.GetValue()
	}
	for k, v := range want {
		if got[k] != v {
			return false
		}
	}
	return true
}

// THE test. A failed Visibility query must not publish a zero.
//
// Publishing 0 on failure would report "nothing over budget, nothing stuck"
// during precisely the outage when that is least likely to be true, and it would
// look exactly like healthy data on the dashboard.
func TestFailedPollDoesNotZeroGauges(t *testing.T) {
	fc := &fakeCounter{answers: map[string]int64{"StartTime <": 7}}
	mo, _, reg := testSetup(t, fc)
	mo.setStuckAvailable(false)

	mo.pollFast(context.Background())
	v, ok := gaugeValue(t, reg, "temporal_slo_over_budget_executions",
		map[string]string{"bucket": "1", "workflow_type": "OrderWorkflow"})
	if !ok || v != 7 {
		t.Fatalf("first poll: over_budget = %v (present=%v), want 7", v, ok)
	}

	// Visibility now fails on every query.
	fc.err = serviceerror.NewUnavailable("visibility down")
	mo.pollFast(context.Background())

	v, ok = gaugeValue(t, reg, "temporal_slo_over_budget_executions",
		map[string]string{"bucket": "1", "workflow_type": "OrderWorkflow"})
	if !ok {
		t.Fatal("gauge disappeared after a failed poll")
	}
	if v != 7 {
		t.Fatalf("failed poll overwrote the gauge with %v; it must retain the last known value (7). "+
			"A zero here reads as 'nothing is over budget' during an outage.", v)
	}
}

func TestFailedPollRecordsErrorTypeAndSkipsFreshness(t *testing.T) {
	fc := &fakeCounter{err: serviceerror.NewResourceExhausted(0, "slow down")}
	mo, _, reg := testSetup(t, fc)
	mo.setStuckAvailable(false)

	mo.pollFast(context.Background())

	if _, ok := gaugeValue(t, reg, "temporal_slo_last_successful_poll_timestamp_seconds",
		map[string]string{"workflow_type": "OrderWorkflow"}); ok {
		t.Error("freshness timestamp was set despite every query failing — " +
			"staleness alerting depends on this NOT being updated")
	}

	fams, _ := reg.Gather()
	var found bool
	for _, f := range fams {
		if f.GetName() != "temporal_slo_poll_errors_total" {
			continue
		}
		for _, mm := range f.GetMetric() {
			if labelsMatch(mm, map[string]string{"error_type": "resource_exhausted"}) {
				found = true
			}
		}
	}
	if !found {
		t.Error("expected poll_errors_total{error_type=\"resource_exhausted\"}")
	}
}

// error_type must stay bounded. Free-text messages would be unbounded
// cardinality in a service whose entire premise is label discipline.
func TestErrorTypeIsBounded(t *testing.T) {
	cases := []struct {
		err  error
		want string
	}{
		{serviceerror.NewResourceExhausted(0, "x"), "resource_exhausted"},
		{serviceerror.NewInvalidArgument("x"), "invalid_argument"},
		{serviceerror.NewNotFound("x"), "not_found"},
		{serviceerror.NewUnavailable("x"), "unavailable"},
		{errors.New("some unique message with an id 8f2a"), "other"},
	}
	for _, c := range cases {
		if got := errorType(c.err); got != c.want {
			t.Errorf("errorType(%v) = %q, want %q", c.err, got, c.want)
		}
	}
}

// On a server without TemporalReportedProblems the stuck gauge must be ABSENT,
// not zero. A zero is indistinguishable from "nothing is stuck".
func TestStuckUnavailablePublishesNoStuckSeries(t *testing.T) {
	fc := &fakeCounter{err: serviceerror.NewInvalidArgument("unknown search attribute TemporalReportedProblems")}
	mo, _, reg := testSetup(t, fc)

	mo.probeStuckDetection(context.Background())

	if mo.StuckAvailable() {
		t.Fatal("probe should have concluded the attribute is unavailable")
	}
	v, ok := gaugeValue(t, reg, "temporal_slo_stuck_detection_available",
		map[string]string{"method": "reported_problems"})
	if !ok || v != 0 {
		t.Fatalf("stuck_detection_available{reported_problems} = %v (present=%v), want 0", v, ok)
	}
	// The duration ladder still works and must say so, otherwise an operator
	// cannot tell "this signal is off" from "the whole monitor is broken".
	if v, ok := gaugeValue(t, reg, "temporal_slo_stuck_detection_available",
		map[string]string{"method": "duration_buckets"}); !ok || v != 1 {
		t.Fatalf("duration_buckets availability = %v (present=%v), want 1", v, ok)
	}

	fc.err = nil
	mo.pollFast(context.Background())
	if _, ok := gaugeValue(t, reg, "temporal_slo_stuck_executions", map[string]string{}); ok {
		t.Error("stuck_executions was published while detection is unavailable; " +
			"a 0 there reads as a clean bill of health")
	}
}

func TestBucketLadderQueriesEveryConfiguredMultiplier(t *testing.T) {
	fc := &fakeCounter{}
	mo, _, _ := testSetup(t, fc)
	mo.setStuckAvailable(false)

	mo.pollFast(context.Background())

	// 1 running + 3 buckets, and no stuck queries.
	if len(fc.queries) != 4 {
		t.Fatalf("expected 4 queries (1 running + 3 buckets), got %d: %v", len(fc.queries), fc.queries)
	}
}

// The SLI's denominator depends on bucket="1" existing as its own series.
func TestOverBudgetBucketOneIsPublishedSeparately(t *testing.T) {
	fc := &fakeCounter{answers: map[string]int64{"StartTime <": 3}}
	mo, _, reg := testSetup(t, fc)
	mo.setStuckAvailable(false)
	mo.pollFast(context.Background())

	for _, b := range []string{"1", "2", "5"} {
		if _, ok := gaugeValue(t, reg, "temporal_slo_over_budget_executions",
			map[string]string{"bucket": b}); !ok {
			t.Errorf("bucket=%s series missing; the SLI denominator needs bucket=1 in particular", b)
		}
	}
}

func TestWindowPollPublishesBothSidesOfCompliance(t *testing.T) {
	fc := &fakeCounter{answers: map[string]int64{
		"ExecutionDuration <":  40,
		"ExecutionDuration >=": 2,
	}}
	mo, _, reg := testSetup(t, fc)

	mo.pollWindow(context.Background())

	if v, ok := gaugeValue(t, reg, "temporal_slo_closed_in_budget", map[string]string{}); !ok || v != 40 {
		t.Errorf("closed_in_budget = %v (present=%v), want 40", v, ok)
	}
	if v, ok := gaugeValue(t, reg, "temporal_slo_closed_over_budget", map[string]string{}); !ok || v != 2 {
		t.Errorf("closed_over_budget = %v (present=%v), want 2", v, ok)
	}
}

// One permanently failing query of a kind must keep that kind's freshness
// stale. Stamped per query, the other buckets kept the timestamp at "now"
// while the failing one sat frozen, and MonitorPollsStale never fired.
func TestFreshnessRequiresEveryQueryOfAKind(t *testing.T) {
	fixed := time.Date(2026, 10, 16, 12, 0, 0, 0, time.UTC)
	cutoff2x := fixed.Add(-2 * time.Hour).Format(time.RFC3339) // budget 1h, bucket 2
	fc := &fakeCounter{answers: map[string]int64{"StartTime <": 3}, failIf: func(q string) error {
		if strings.Contains(q, cutoff2x) {
			return serviceerror.NewUnavailable("this one query keeps failing")
		}
		return nil
	}}
	mo, _, reg := testSetup(t, fc)
	mo.now = func() time.Time { return fixed }
	mo.setStuckAvailable(false)

	mo.pollFast(context.Background())

	if _, ok := gaugeValue(t, reg, "temporal_slo_last_successful_poll_timestamp_seconds",
		map[string]string{"workflow_type": "OrderWorkflow", "query_kind": "over_budget"}); ok {
		t.Error("over_budget freshness was stamped although its 2x query failed")
	}
	if _, ok := gaugeValue(t, reg, "temporal_slo_last_successful_poll_timestamp_seconds",
		map[string]string{"workflow_type": "OrderWorkflow", "query_kind": "running"}); !ok {
		t.Error("running freshness should be stamped: its only query succeeded")
	}
}

// The window poll publishes the three SLI terms together or not at all. Two
// fresh terms beside one stale term would recreate the skew the snapshot
// exists to remove.
func TestWindowPollIsAllOrNothing(t *testing.T) {
	fc := &fakeCounter{answers: map[string]int64{
		"ExecutionDuration <":  10, // closed in budget
		"ExecutionDuration >=": 3,  // closed over budget
		"StartTime <":          2,  // open past 1x budget, same instant
	}}
	mo, _, reg := testSetup(t, fc)
	mo.pollWindow(context.Background())
	if v, _ := gaugeValue(t, reg, "temporal_slo_over_budget_executions_at_window_poll",
		map[string]string{"workflow_type": "OrderWorkflow"}); v != 2 {
		t.Fatalf("open snapshot = %v, want 2", v)
	}

	// Next cycle: closed-in would read 20, but the open snapshot fails.
	fc.answers["ExecutionDuration <"] = 20
	fc.failIf = func(q string) error {
		if strings.Contains(q, "StartTime <") {
			return serviceerror.NewUnavailable("down")
		}
		return nil
	}
	mo.pollWindow(context.Background())
	if v, _ := gaugeValue(t, reg, "temporal_slo_closed_in_budget",
		map[string]string{"workflow_type": "OrderWorkflow"}); v != 10 {
		t.Fatalf("closed_in_budget = %v after a partial failure, want the previous 10 — "+
			"publishing it beside a stale open term skews compliance", v)
	}
}

// A transient error during the startup probe must not disable stuck detection
// for the life of the process.
func TestTransientProbeErrorIsRetried(t *testing.T) {
	down := true
	fc := &fakeCounter{failIf: func(q string) error {
		if down && strings.Contains(q, "TemporalReportedProblems") {
			return serviceerror.NewUnavailable("frontend restarting")
		}
		return nil
	}}
	mo, _, _ := testSetup(t, fc)

	mo.probeStuckDetection(context.Background())
	if mo.StuckAvailable() || !mo.ProbePending() {
		t.Fatalf("after a transient error: available=%v pending=%v, want false/true",
			mo.StuckAvailable(), mo.ProbePending())
	}

	down = false
	mo.pollFast(context.Background())
	if !mo.StuckAvailable() || mo.ProbePending() {
		t.Fatalf("after recovery: available=%v pending=%v, want true/false",
			mo.StuckAvailable(), mo.ProbePending())
	}
}

// A definitive rejection settles it: no re-probing on every poll.
func TestDefinitiveProbeRejectionSettles(t *testing.T) {
	fc := &fakeCounter{failIf: func(q string) error {
		if strings.Contains(q, "TemporalReportedProblems") {
			return serviceerror.NewInvalidArgument("unknown search attribute")
		}
		return nil
	}}
	mo, _, _ := testSetup(t, fc)
	mo.probeStuckDetection(context.Background())
	if mo.StuckAvailable() || mo.ProbePending() {
		t.Fatalf("available=%v pending=%v, want false/false", mo.StuckAvailable(), mo.ProbePending())
	}
	before := len(fc.queries)
	mo.pollFast(context.Background())
	for _, q := range fc.queries[before:] {
		if strings.Contains(q, "TemporalReportedProblems") {
			t.Fatal("re-probed after a definitive rejection")
		}
	}
}
