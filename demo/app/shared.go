package main

import (
	"os"
	"strconv"
)

// OrderInput carries the chaos levers into the Workflow. Every field here maps
// to a failure mode that shows up on a specific dashboard panel — see
// docs/CHAOS-RUNBOOK.md for which lever moves which panel.
// StuckReleaseSignal releases a Workflow parked by StuckMode="parked".
const StuckReleaseSignal = "resume"

type OrderInput struct {
	OrderID string `json:"orderId"`

	// FailureRate is the per-attempt probability (0.0–1.0) that ChargePayment
	// returns an error. Drives temporal_activity_execution_failed and, once
	// retries are exhausted, workflow_failed.
	FailureRate float64 `json:"failureRate"`

	// ActivityDelayMs is artificial latency inside ChargePayment. Holds an
	// Activity slot open, which is how you drive temporal_worker_task_slots_available
	// to zero without needing real load.
	ActivityDelayMs int `json:"activityDelayMs"`

	// StuckMode stalls the Workflow in a specific, named way. Empty means
	// normal execution.
	//
	//   "parked"      Blocks forever on a Signal the lab never sends. The
	//                 Workflow is Running, pollers are healthy, nothing has
	//                 failed and nothing is retrying. THIS IS THE INVISIBLE
	//                 CASE: no metric moves and no alert fires, which is
	//                 precisely why external duration monitoring exists.
	//                 Release it with `make chaos-stuck-release`.
	//
	//   "retry-storm" An Activity that fails on every attempt with UNLIMITED
	//                 retries. The Workflow never fails and never finishes;
	//                 it burns Actions forever. Visible as a rising Activity
	//                 failure rate long before anything else notices.
	StuckMode string `json:"stuckMode"`

	// MaxAttempts caps the Activity retry policy. Set to 1 to make Activity
	// failures convert straight into Workflow failures (a deliberately bad
	// error-handling posture, useful for demonstrating the failure conversion
	// rate discussed in the guide).
	MaxAttempts int32 `json:"maxAttempts"`
}

const (
	defaultTaskQueue     = "orders"
	workflowIDPrefix     = "order-"
	activityStartToClose = 60 // seconds
)

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return fallback
}
