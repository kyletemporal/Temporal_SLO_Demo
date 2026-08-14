// Package metrics defines every series the monitor publishes.
//
// LABEL DISCIPLINE IS THE POINT OF THIS FILE. There is no workflow_id and no
// run_id here, ever. Temporal omits workflow_id from SDK metrics deliberately
// because it is unbounded; reintroducing it in a service that queries Visibility
// would blow up cardinality in exactly the same way. When an operator needs the
// specific execution, the answer is a Visibility query or the logs — not a
// label. See the "Find stuck executions" panel.
//
// Every label here is bounded: workflow_type and task_queue come from config,
// bucket from the configured multipliers, cause from Temporal's enum.
package metrics

import "github.com/prometheus/client_golang/prometheus"

const ns = "temporal_slo"

// Query kinds, used as a label and for per-kind staleness.
const (
	KindRunning    = "running"
	KindOverBudget = "over_budget"
	KindStuck      = "stuck"
	KindClosed     = "closed_window"
)

type Metrics struct {
	Running         *prometheus.GaugeVec
	OverBudget      *prometheus.GaugeVec
	Stuck           *prometheus.GaugeVec
	ClosedInBudget  *prometheus.GaugeVec
	ClosedOver      *prometheus.GaugeVec
	BudgetSeconds   *prometheus.GaugeVec
	Objective       *prometheus.GaugeVec
	DetectionAvail  *prometheus.GaugeVec
	PollDuration    *prometheus.HistogramVec
	PollErrors      *prometheus.CounterVec
	LastSuccessPoll *prometheus.GaugeVec
}

func New(reg prometheus.Registerer) *Metrics {
	m := &Metrics{
		Running: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Namespace: ns, Name: "running_executions",
			Help: "Open executions, by type and task queue.",
		}, []string{"workflow_type", "task_queue"}),

		// bucket="1" is load-bearing. It is the term that puts a still-running
		// over-budget execution into the SLI denominator, which is what stops
		// terminating a stuck Workflow from improving the compliance number.
		OverBudget: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Namespace: ns, Name: "over_budget_executions",
			Help: "Open executions running past N x budget.",
		}, []string{"workflow_type", "task_queue", "bucket"}),

		Stuck: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Namespace: ns, Name: "stuck_executions",
			Help: "Executions Temporal itself reports as having a problem.",
		}, []string{"workflow_type", "task_queue", "cause"}),

		ClosedInBudget: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Namespace: ns, Name: "closed_in_budget",
			Help: "Executions closed within budget over the SLO window.",
		}, []string{"workflow_type", "task_queue"}),

		ClosedOver: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Namespace: ns, Name: "closed_over_budget",
			Help: "Executions closed over budget over the SLO window.",
		}, []string{"workflow_type", "task_queue"}),

		// Exported so recording rules read the budget from the series instead of
		// hardcoding it. Changing a budget becomes a config edit that propagates
		// without regenerating any rules.
		BudgetSeconds: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Namespace: ns, Name: "budget_seconds",
			Help: "Configured duration budget, in seconds.",
		}, []string{"workflow_type"}),

		// Exported for the same reason as BudgetSeconds: error-budget rules need
		// the objective, and hardcoding 0.99 in a rule file means the config and
		// the alert can drift apart silently.
		Objective: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Namespace: ns, Name: "objective_ratio",
			Help: "Configured objective for this workflow type, as a ratio.",
		}, []string{"workflow_type"}),

		// 0 means THIS SIGNAL DOES NOT WORK HERE, which is a completely different
		// statement from "nothing is stuck". Without it, stuck_executions sitting
		// at 0 on a server that lacks TemporalReportedProblems is indistinguishable
		// from a clean bill of health — the exact failure this repo keeps finding.
		DetectionAvail: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Namespace: ns, Name: "stuck_detection_available",
			Help: "1 if the named stuck-detection method works on this server, else 0.",
		}, []string{"namespace", "method"}),

		PollDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Namespace: ns, Name: "poll_duration_seconds",
			Help:    "Visibility query latency.",
			Buckets: []float64{.05, .1, .25, .5, 1, 2.5, 5, 10, 30},
		}, []string{"workflow_type", "query_kind"}),

		PollErrors: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: ns, Name: "poll_errors_total",
			Help: "Failed Visibility queries.",
		}, []string{"workflow_type", "query_kind", "error_type"}),

		// The staleness signal, and it is not optional. The gauges above are
		// deliberately NOT reset when a poll fails (see monitor.go), so this
		// timestamp is the only way to distinguish a current zero from a zero
		// frozen since the last successful query.
		LastSuccessPoll: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Namespace: ns, Name: "last_successful_poll_timestamp_seconds",
			Help: "Unix time of the last successful poll, per type and query kind.",
		}, []string{"workflow_type", "query_kind"}),
	}
	reg.MustRegister(m.Running, m.OverBudget, m.Stuck, m.ClosedInBudget,
		m.ClosedOver, m.BudgetSeconds, m.Objective, m.DetectionAvail, m.PollDuration,
		m.PollErrors, m.LastSuccessPoll)
	return m
}
