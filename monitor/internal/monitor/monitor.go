// Package monitor runs the poll loops that turn Visibility counts into metrics.
//
// Two loops at different rates, because the queries cost different amounts:
//
//   - fast (poll_interval, default 60s): running, over-budget ladder, stuck.
//     These are cheap and drive alerting.
//   - window (window_poll_interval, default 600s): closed-in-budget and
//     closed-over-budget across the whole SLO window. These scan far more data
//     and drive compliance, which nobody needs at minute resolution.
package monitor

import (
	"context"
	"errors"
	"log/slog"
	"strconv"
	"sync"
	"time"

	"go.temporal.io/api/serviceerror"

	"github.com/temporal-slo-demo/monitor/internal/config"
	"github.com/temporal-slo-demo/monitor/internal/counter"
	"github.com/temporal-slo-demo/monitor/internal/metrics"
	"github.com/temporal-slo-demo/monitor/internal/visibility"
)

type Monitor struct {
	cfg *config.Config
	c   counter.Counter
	m   *metrics.Metrics
	log *slog.Logger

	// now is injectable so boundary arithmetic is testable.
	now func() time.Time

	mu             sync.RWMutex
	stuckAvailable bool
}

func New(cfg *config.Config, c counter.Counter, m *metrics.Metrics, log *slog.Logger) *Monitor {
	return &Monitor{cfg: cfg, c: c, m: m, log: log, now: func() time.Time { return time.Now().UTC() }}
}

// Run publishes budgets, probes stuck detection, then runs both loops until ctx
// is cancelled. It polls once immediately so a restart does not leave the
// dashboard empty for a whole interval.
func (mo *Monitor) Run(ctx context.Context) error {
	for _, wt := range mo.cfg.WorkflowTypes {
		mo.m.BudgetSeconds.WithLabelValues(wt.Name).Set(wt.Budget.Std().Seconds())
	}

	mo.probeStuckDetection(ctx)

	fast := time.NewTicker(mo.cfg.Defaults.PollInterval.Std())
	defer fast.Stop()
	window := time.NewTicker(mo.cfg.Defaults.WindowPollInterval.Std())
	defer window.Stop()

	mo.pollFast(ctx)
	mo.pollWindow(ctx)

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-fast.C:
			mo.pollFast(ctx)
		case <-window.C:
			mo.pollWindow(ctx)
		}
	}
}

// probeStuckDetection establishes whether TemporalReportedProblems works here,
// by running the query once and seeing whether the server rejects it.
//
// This is a capability probe, not an assumption. The attribute needs Server 1.30+
// AND dynamic config when self-hosting, so the common case — including the 1.27.4
// this lab defaults to — is that it does not exist. A rejected query surfaces as
// InvalidArgument/NotFound rather than a zero count, which is the good failure
// mode: the alternative would be a stuck_executions gauge pinned at 0 that reads
// exactly like "nothing is stuck".
func (mo *Monitor) probeStuckDetection(ctx context.Context) {
	if mo.cfg.Deployment.StuckDetection == config.StuckFallback {
		mo.setStuckAvailable(false)
		mo.log.Info("stuck detection disabled by config; duration buckets only",
			"stuck_detection", string(mo.cfg.Deployment.StuckDetection))
		mo.m.DetectionAvail.WithLabelValues(mo.cfg.Deployment.Namespace, "reported_problems").Set(0)
		return
	}

	probeType := "__slo_monitor_probe__"
	if len(mo.cfg.WorkflowTypes) > 0 {
		probeType = mo.cfg.WorkflowTypes[0].Name
	}

	_, err := mo.c.Count(ctx, visibility.StuckByCause("", "", visibility.ReportedProblemCauses[0]))
	if err == nil {
		_, err = mo.c.Count(ctx, visibility.Stuck(probeType, ""))
	}

	available := err == nil
	mo.setStuckAvailable(available)
	mo.m.DetectionAvail.WithLabelValues(mo.cfg.Deployment.Namespace, "reported_problems").Set(boolToFloat(available))
	// The duration ladder always works: it needs only StartTime, which every
	// execution has. It is what makes this service useful on servers below 1.30.
	mo.m.DetectionAvail.WithLabelValues(mo.cfg.Deployment.Namespace, "duration_buckets").Set(1)

	if available {
		mo.log.Info("TemporalReportedProblems available; stuck detection enabled")
		return
	}
	if mo.cfg.Deployment.StuckDetection == config.StuckReportedProblems {
		mo.log.Error("stuck_detection is set to reported_problems but the search attribute is unavailable; "+
			"stuck_executions will not be published. Set stuck_detection: auto to fall back to duration buckets.",
			"error", err)
		return
	}
	mo.log.Warn("TemporalReportedProblems unavailable (needs Server 1.30+ and, self-hosted, "+
		"system.numConsecutiveWorkflowTaskProblemsToTriggerSearchAttribute); "+
		"falling back to duration buckets only",
		"error", err)
}

func (mo *Monitor) setStuckAvailable(v bool) {
	mo.mu.Lock()
	defer mo.mu.Unlock()
	mo.stuckAvailable = v
}

func (mo *Monitor) StuckAvailable() bool {
	mo.mu.RLock()
	defer mo.mu.RUnlock()
	return mo.stuckAvailable
}

func (mo *Monitor) pollFast(ctx context.Context) {
	now := mo.now()
	for _, wt := range mo.cfg.WorkflowTypes {
		if n, ok := mo.count(ctx, wt.Name, metrics.KindRunning, visibility.Running(wt.Name, wt.TaskQueue)); ok {
			mo.m.Running.WithLabelValues(wt.Name, wt.TaskQueue).Set(float64(n))
		}

		for _, mult := range mo.cfg.Defaults.Buckets {
			q := visibility.OverBudget(wt.Name, wt.TaskQueue, wt.Budget.Std(), mult, now)
			if n, ok := mo.count(ctx, wt.Name, metrics.KindOverBudget, q); ok {
				mo.m.OverBudget.WithLabelValues(wt.Name, wt.TaskQueue, strconv.Itoa(mult)).Set(float64(n))
			}
		}

		if !mo.StuckAvailable() {
			continue
		}
		for _, cause := range visibility.ReportedProblemCauses {
			q := visibility.StuckByCause(wt.Name, wt.TaskQueue, cause)
			if n, ok := mo.count(ctx, wt.Name, metrics.KindStuck, q); ok {
				mo.m.Stuck.WithLabelValues(wt.Name, wt.TaskQueue, cause).Set(float64(n))
			}
		}
	}
}

func (mo *Monitor) pollWindow(ctx context.Context) {
	now := mo.now()
	statuses := mo.cfg.Defaults.ClosedOverBudgetStatuses
	win := mo.cfg.Defaults.SLOWindow.Std()

	for _, wt := range mo.cfg.WorkflowTypes {
		budget := wt.Budget.Std()

		inQ := visibility.ClosedInWindow(wt.Name, wt.TaskQueue, budget, true, statuses, win, now)
		if n, ok := mo.count(ctx, wt.Name, metrics.KindClosed, inQ); ok {
			mo.m.ClosedInBudget.WithLabelValues(wt.Name, wt.TaskQueue).Set(float64(n))
		}

		overQ := visibility.ClosedInWindow(wt.Name, wt.TaskQueue, budget, false, statuses, win, now)
		if n, ok := mo.count(ctx, wt.Name, metrics.KindClosed, overQ); ok {
			mo.m.ClosedOver.WithLabelValues(wt.Name, wt.TaskQueue).Set(float64(n))
		}
	}
}

// count runs one query and records latency, errors and freshness.
//
// ON FAILURE IT RETURNS ok=false AND THE CALLER LEAVES THE GAUGE ALONE. This is
// the most important behaviour in the file. Publishing 0 for a failed query
// would report "nothing is stuck, nothing is over budget" during exactly the
// outage where that is least likely to be true, and it would do so in a way that
// looks like healthy data. Prometheus keeps the previous value instead, and
// last_successful_poll_timestamp_seconds is what tells you it is stale — alert
// on that, or the frozen value becomes its own silent failure.
func (mo *Monitor) count(ctx context.Context, wfType, kind, query string) (int64, bool) {
	start := mo.now()
	n, err := mo.c.Count(ctx, query)
	mo.m.PollDuration.WithLabelValues(wfType, kind).Observe(mo.now().Sub(start).Seconds())

	if err != nil {
		if ctx.Err() != nil {
			return 0, false // shutting down; not a real poll failure
		}
		mo.m.PollErrors.WithLabelValues(wfType, kind, errorType(err)).Inc()
		mo.log.Warn("visibility query failed; leaving previous gauge value in place",
			"workflow_type", wfType, "query_kind", kind, "error", err)
		return 0, false
	}

	mo.m.LastSuccessPoll.WithLabelValues(wfType, kind).Set(float64(mo.now().Unix()))
	return n, true
}

// errorType keeps the error_type label bounded to Temporal's service-error kinds
// rather than free-text messages, which would be unbounded cardinality.
func errorType(err error) string {
	switch {
	case errorIs[*serviceerror.ResourceExhausted](err):
		return "resource_exhausted"
	case errorIs[*serviceerror.InvalidArgument](err):
		return "invalid_argument"
	case errorIs[*serviceerror.NotFound](err):
		return "not_found"
	case errorIs[*serviceerror.Unavailable](err):
		return "unavailable"
	case errorIs[*serviceerror.DeadlineExceeded](err):
		return "deadline_exceeded"
	case errorIs[*serviceerror.PermissionDenied](err):
		return "permission_denied"
	default:
		return "other"
	}
}

func errorIs[T error](err error) bool {
	var t T
	return errors.As(err, &t)
}

func boolToFloat(b bool) float64 {
	if b {
		return 1
	}
	return 0
}
