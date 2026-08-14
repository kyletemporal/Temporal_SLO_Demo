// Package visibility builds Temporal Visibility list-filter queries.
//
// Query construction lives here, isolated and heavily tested, because the
// timestamp arithmetic in the age ladder is the single easiest place in this
// service to be silently wrong. An off-by-one on a bucket boundary does not
// crash anything — it quietly moves executions between SLO buckets and the
// compliance ratio is wrong in a way nobody notices for a month.
package visibility

import (
	"fmt"
	"strings"
	"time"
)

// Status values Temporal reports for a Workflow Execution.
const (
	StatusRunning    = "Running"
	StatusCompleted  = "Completed"
	StatusFailed     = "Failed"
	StatusCanceled   = "Canceled"
	StatusTerminated = "Terminated"
	StatusTimedOut   = "TimedOut"
)

// ReportedProblemCauses are the values Temporal writes into the
// TemporalReportedProblems search attribute. Available from Server 1.30, and on
// self-hosted only when
// system.numConsecutiveWorkflowTaskProblemsToTriggerSearchAttribute is set.
var ReportedProblemCauses = []string{
	"category=WorkflowTaskFailed",
	"category=WorkflowTaskTimedOut",
}

// quote escapes a value for use inside a single-quoted list-filter literal.
func quote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", "\\'") + "'"
}

// Running counts every open execution of a type. This is the SLI denominator
// baseline and a dashboard line. It is deliberately NOT alertable on its own:
// the absolute number of running workflows scales with business volume, not
// with health.
func Running(workflowType, taskQueue string) string {
	return join(
		"ExecutionStatus = "+quote(StatusRunning),
		"WorkflowType = "+quote(workflowType),
		taskQueueClause(taskQueue),
	)
}

// Stuck counts open executions Temporal has flagged as not making progress.
//
// Requires the TemporalReportedProblems search attribute — Server 1.30+, and
// enabled via dynamic config when self-hosting. Callers must check capability
// first; on a cluster without it this query is rejected rather than returning
// zero, which is the good failure mode.
func Stuck(workflowType, taskQueue string) string {
	vals := make([]string, len(ReportedProblemCauses))
	for i, c := range ReportedProblemCauses {
		vals[i] = quote(c)
	}
	return join(
		"ExecutionStatus = "+quote(StatusRunning),
		"WorkflowType = "+quote(workflowType),
		taskQueueClause(taskQueue),
		"TemporalReportedProblems IN ("+strings.Join(vals, ", ")+")",
	)
}

// StuckByCause is Stuck narrowed to a single reported cause, so the exported
// metric can carry a real `cause` label instead of a constant.
//
// One query per cause rather than one aggregate: WorkflowTaskFailed and
// WorkflowTaskTimedOut have different remedies (a code bug versus a Worker that
// cannot finish in time), and collapsing them costs the operator the one piece
// of information that decides what to do next. The cause set is fixed at two, so
// this doubles a small number.
func StuckByCause(workflowType, taskQueue, cause string) string {
	return join(
		"ExecutionStatus = "+quote(StatusRunning),
		"WorkflowType = "+quote(workflowType),
		taskQueueClause(taskQueue),
		"TemporalReportedProblems = "+quote(cause),
	)
}

// OverBudget counts open executions that started longer ago than
// multiplier × budget — i.e. they have been running past that many multiples of
// the promise and have still not closed.
//
// The comparison is on StartTime, not on a duration: ExecutionDuration only
// exists on CLOSED executions, which is the entire reason this service exists.
//
// now is passed in rather than read from the clock so the boundary arithmetic
// is testable. Truncated to whole seconds: Visibility timestamps have limited
// precision and sub-second jitter would make bucket membership flap between
// polls for executions sitting exactly on a boundary.
func OverBudget(workflowType, taskQueue string, budget time.Duration, multiplier int, now time.Time) string {
	cutoff := now.Add(-time.Duration(multiplier) * budget).UTC().Truncate(time.Second)
	return join(
		"ExecutionStatus = "+quote(StatusRunning),
		"WorkflowType = "+quote(workflowType),
		taskQueueClause(taskQueue),
		"StartTime < "+quote(cutoff.Format(time.RFC3339)),
	)
}

// ClosedInWindow counts executions of a type that closed inside the SLO
// compliance window, split by whether they finished within budget.
//
// ExecutionDuration is in NANOSECONDS and exists only on closed executions.
// inBudget=true  -> duration <  budget   (compliant)
// inBudget=false -> duration >= budget   (a violation that has already landed)
//
// statuses is the set of terminal states that count. Terminated and Canceled
// belong there: if terminating a late workflow removed it from the denominator,
// the SLO would improve as we destroyed the evidence.
func ClosedInWindow(workflowType, taskQueue string, budget time.Duration, inBudget bool, statuses []string, window time.Duration, now time.Time) string {
	from := now.Add(-window).UTC().Truncate(time.Second)

	cmp := "<"
	if !inBudget {
		cmp = ">="
	}

	return join(
		statusClause(statuses),
		"WorkflowType = "+quote(workflowType),
		taskQueueClause(taskQueue),
		"CloseTime >= "+quote(from.Format(time.RFC3339)),
		fmt.Sprintf("ExecutionDuration %s %d", cmp, budget.Nanoseconds()),
	)
}

// ClosedDurationAtMost counts closed executions of a type, within the lookback,
// whose duration is at most d. Budget derivation binary-searches on this to
// find percentiles without paging every execution.
func ClosedDurationAtMost(workflowType string, d time.Duration, lookback time.Duration, now time.Time) string {
	from := now.Add(-lookback).UTC().Truncate(time.Second)
	return join(
		statusClause([]string{StatusCompleted}),
		"WorkflowType = "+quote(workflowType),
		"CloseTime >= "+quote(from.Format(time.RFC3339)),
		fmt.Sprintf("ExecutionDuration <= %d", d.Nanoseconds()),
	)
}

// ClosedTotal counts all closed executions of a type in the lookback. The
// denominator for percentile derivation.
func ClosedTotal(workflowType string, lookback time.Duration, now time.Time) string {
	from := now.Add(-lookback).UTC().Truncate(time.Second)
	return join(
		statusClause([]string{StatusCompleted}),
		"WorkflowType = "+quote(workflowType),
		"CloseTime >= "+quote(from.Format(time.RFC3339)),
	)
}

func statusClause(statuses []string) string {
	if len(statuses) == 1 {
		return "ExecutionStatus = " + quote(statuses[0])
	}
	q := make([]string, len(statuses))
	for i, s := range statuses {
		q[i] = quote(s)
	}
	return "ExecutionStatus IN (" + strings.Join(q, ", ") + ")"
}

// taskQueueClause is optional: a workflow type may legitimately span queues, and
// over-constraining silently drops executions from the SLI.
func taskQueueClause(taskQueue string) string {
	if taskQueue == "" {
		return ""
	}
	return "TaskQueue = " + quote(taskQueue)
}

func join(clauses ...string) string {
	out := make([]string, 0, len(clauses))
	for _, c := range clauses {
		if c != "" {
			out = append(out, c)
		}
	}
	return strings.Join(out, " AND ")
}
