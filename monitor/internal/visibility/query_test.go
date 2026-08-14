package visibility

import (
	"strings"
	"testing"
	"time"
)

// Fixed reference time. Everything below is arithmetic against this, because
// "now" in a test is how boundary bugs hide.
var now = time.Date(2026, 8, 13, 12, 0, 0, 0, time.UTC)

func TestOverBudget_LadderBoundaries(t *testing.T) {
	budget := 4 * time.Hour

	// The whole point of the ladder: each multiplier must cut at exactly
	// multiplier * budget before now. An off-by-one here moves executions
	// between SLO buckets and nothing crashes.
	cases := []struct {
		multiplier int
		wantCutoff string
	}{
		{1, "2026-08-13T08:00:00Z"}, // now - 4h
		{2, "2026-08-13T04:00:00Z"}, // now - 8h
		{5, "2026-08-12T16:00:00Z"}, // now - 20h, crosses the day boundary
	}

	for _, c := range cases {
		got := OverBudget("OrderWorkflow", "orders", budget, c.multiplier, now)
		want := "StartTime < '" + c.wantCutoff + "'"
		if !strings.Contains(got, want) {
			t.Errorf("multiplier %d: want cutoff %q\n  in: %s", c.multiplier, want, got)
		}
	}
}

func TestOverBudget_UsesStartTimeNotDuration(t *testing.T) {
	// ExecutionDuration does not exist on running executions. If this query
	// ever references it, the age ladder silently returns zero for everything
	// and stuck workflows become invisible — the exact failure this service
	// exists to prevent.
	got := OverBudget("OrderWorkflow", "orders", time.Hour, 1, now)
	if strings.Contains(got, "ExecutionDuration") {
		t.Fatalf("OverBudget must filter on StartTime, not ExecutionDuration: %s", got)
	}
	if !strings.Contains(got, "ExecutionStatus = 'Running'") {
		t.Errorf("OverBudget must be scoped to Running: %s", got)
	}
}

func TestOverBudget_TruncatesSubSecond(t *testing.T) {
	// Sub-second jitter would make an execution sitting on a boundary flap in
	// and out of a bucket between polls.
	jittery := now.Add(456 * time.Millisecond)
	got := OverBudget("OrderWorkflow", "", time.Hour, 1, jittery)
	if !strings.Contains(got, "'2026-08-13T11:00:00Z'") {
		t.Errorf("sub-second component must be truncated: %s", got)
	}
}

func TestClosedInWindow_BudgetComparisonDirection(t *testing.T) {
	budget := 90 * time.Minute
	wantNanos := budget.Nanoseconds() // 5400000000000

	in := ClosedInWindow("OrderWorkflow", "orders", budget, true,
		[]string{StatusCompleted}, 28*24*time.Hour, now)
	if !strings.Contains(in, "ExecutionDuration < 5400000000000") {
		t.Errorf("in-budget must be strictly less than budget in NANOSECONDS: %s", in)
	}

	over := ClosedInWindow("OrderWorkflow", "orders", budget, false,
		[]string{StatusCompleted}, 28*24*time.Hour, now)
	if !strings.Contains(over, "ExecutionDuration >= 5400000000000") {
		t.Errorf("over-budget must be >= budget: %s", over)
	}

	// The two must partition the space exactly. Any overlap double-counts and
	// any gap loses executions from the denominator.
	//
	// Assert on the ExecutionDuration clause specifically: both queries also
	// contain "CloseTime >=", so a bare search for ">=" matches the window
	// bound and reports a false conflict.
	if strings.Contains(in, "ExecutionDuration >=") {
		t.Errorf("in-budget must not use >=: %s", in)
	}
	if strings.Contains(over, "ExecutionDuration < ") {
		t.Errorf("over-budget must not use <: %s", over)
	}
	_ = wantNanos
}

func TestClosedInWindow_TerminalStatusesCountAsViolations(t *testing.T) {
	// If Terminated were excluded, terminating a late workflow would remove it
	// from the denominator and the SLO would IMPROVE as we destroyed the
	// evidence. That is the failure mode this whole design guards against.
	statuses := []string{StatusCompleted, StatusFailed, StatusCanceled, StatusTerminated, StatusTimedOut}
	got := ClosedInWindow("OrderWorkflow", "", time.Hour, false, statuses, 28*24*time.Hour, now)

	for _, s := range statuses {
		if !strings.Contains(got, "'"+s+"'") {
			t.Errorf("terminal status %q missing from over-budget query: %s", s, got)
		}
	}
	if !strings.Contains(got, "ExecutionStatus IN (") {
		t.Errorf("multiple statuses must use IN: %s", got)
	}
}

func TestClosedInWindow_WindowStart(t *testing.T) {
	got := ClosedInWindow("OrderWorkflow", "", time.Hour, true,
		[]string{StatusCompleted}, 28*24*time.Hour, now)
	// now - 28d
	if !strings.Contains(got, "CloseTime >= '2026-07-16T12:00:00Z'") {
		t.Errorf("28d window start wrong: %s", got)
	}
}

func TestStuck_UsesBothReportedCauses(t *testing.T) {
	got := Stuck("OrderWorkflow", "orders")
	for _, c := range ReportedProblemCauses {
		if !strings.Contains(got, "'"+c+"'") {
			t.Errorf("missing cause %q: %s", c, got)
		}
	}
	if !strings.Contains(got, "TemporalReportedProblems IN (") {
		t.Errorf("must use IN over the KeywordList: %s", got)
	}
	if !strings.Contains(got, "ExecutionStatus = 'Running'") {
		t.Errorf("stuck is only meaningful for Running executions: %s", got)
	}
}

func TestTaskQueueOptional(t *testing.T) {
	// A workflow type may span task queues. Over-constraining silently drops
	// executions from the SLI, so an empty task queue must omit the clause
	// rather than match the empty string.
	with := Running("OrderWorkflow", "orders")
	without := Running("OrderWorkflow", "")

	if !strings.Contains(with, "TaskQueue = 'orders'") {
		t.Errorf("task queue should be included when set: %s", with)
	}
	if strings.Contains(without, "TaskQueue") {
		t.Errorf("task queue clause must be omitted when empty, not matched as '': %s", without)
	}
	if strings.HasSuffix(without, "AND") || strings.Contains(without, "AND  AND") {
		t.Errorf("empty clause left a dangling AND: %q", without)
	}
}

func TestQuoteEscaping(t *testing.T) {
	got := Running("Order'Workflow", "")
	if !strings.Contains(got, `'Order\'Workflow'`) {
		t.Errorf("single quotes must be escaped to avoid breaking the filter: %s", got)
	}
}

func TestClosedDurationAtMost_ForPercentileSearch(t *testing.T) {
	got := ClosedDurationAtMost("OrderWorkflow", 250*time.Millisecond, 30*24*time.Hour, now)
	if !strings.Contains(got, "ExecutionDuration <= 250000000") {
		t.Errorf("duration bound must be nanoseconds: %s", got)
	}
	if !strings.Contains(got, "CloseTime >= '2026-07-14T12:00:00Z'") {
		t.Errorf("30d lookback wrong: %s", got)
	}
}
