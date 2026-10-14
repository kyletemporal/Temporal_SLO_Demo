package main

import (
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
)

// LaneConfig is everything the UI can change for one lane. A lane is one Task
// Queue served by Workers of one SDK ("go" or "python").
//
// Fields fall into three groups by how a change takes effect, and the UI says
// which:
//
//	live     Profile, DurationMs, JitterPct, IOConcurrency, Rate, CPUs (Go):
//	         read on every Activity / every tick, no restart.
//	swap     WF/Act Pollers and Slots: the Worker is replaced in place — the
//	         new one starts polling before the old one drains.
//	restart  CacheSize (Go only): the Go SDK's sticky cache is process-wide
//	         and sized once, so the Worker process exits and is restarted.
//	         Python caches per Worker, so it swaps instead.
//
// Workflow shape (Phases, ActivitiesPerPhase) applies to Workflows started
// AFTER the change; running ones keep the shape they started with, because a
// Workflow's code path must be deterministic.
type LaneConfig struct {
	Enabled            bool    `json:"enabled"`
	Rate               float64 `json:"rate"` // Workflow starts per second
	Phases             int     `json:"phases"`
	ActivitiesPerPhase int     `json:"activitiesPerPhase"`

	WFPollers  int `json:"wfPollers"`
	WFSlots    int `json:"wfSlots"`
	ActPollers int `json:"actPollers"`
	ActSlots   int `json:"actSlots"`
	CacheSize  int `json:"cacheSize"`
	CPUs       int `json:"cpus"` // Go: GOMAXPROCS. Python: informational (GIL)

	Profile       string `json:"profile"` // "sleep" | "cpu" | "io"
	DurationMs    int    `json:"durationMs"`
	JitterPct     int    `json:"jitterPct"`
	IOConcurrency int    `json:"ioConcurrency"` // downstream service limit, "io" profile
}

// LaneConfigVersioned is what a Worker receives when it polls for config.
type LaneConfigVersioned struct {
	Version int64      `json:"version"`
	Config  LaneConfig `json:"config"`
}

// WorkflowInput is frozen at start. ActivityInput carries nothing about HOW to
// run: the Activity reads the Worker's live config, so a profile change in the
// UI affects work already in flight.
type WorkflowInput struct {
	Lane               string `json:"lane"`
	Phases             int    `json:"phases"`
	ActivitiesPerPhase int    `json:"activitiesPerPhase"`
}

type ActivityInput struct {
	Lane  string `json:"lane"`
	Phase int    `json:"phase"`
	Index int    `json:"index"`
}

const (
	workflowName = "LabWorkflow"
	activityName = "LabActivity"
)

func taskQueueFor(lane string) string { return "lab-" + lane }

// LabWorkflow: Phases rounds of ActivitiesPerPhase parallel Activities. One
// Workflow Task per phase plus one to finish, so the Workflow-Task lane and
// the Activity lane both carry real load and the ratio between them is set
// by the shape.
func LabWorkflow(ctx workflow.Context, in WorkflowInput) (int, error) {
	ctx = workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: 5 * time.Minute,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval: time.Second,
			MaximumAttempts: 3,
		},
	})
	done := 0
	for p := 0; p < in.Phases; p++ {
		futures := make([]workflow.Future, 0, in.ActivitiesPerPhase)
		for i := 0; i < in.ActivitiesPerPhase; i++ {
			futures = append(futures, workflow.ExecuteActivity(ctx, activityName,
				ActivityInput{Lane: in.Lane, Phase: p, Index: i}))
		}
		for _, f := range futures {
			if err := f.Get(ctx, nil); err != nil {
				return done, err
			}
			done++
		}
	}
	return done, nil
}
