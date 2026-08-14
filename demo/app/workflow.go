package main

import (
	"os"
	"context"
	"fmt"
	"math/rand"
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
)

// OrderWorkflow is a three-step order pipeline. It is deliberately boring —
// the interesting behaviour comes from the levers in OrderInput, not from the
// business logic.
func OrderWorkflow(ctx workflow.Context, in OrderInput) (string, error) {
	maxAttempts := in.MaxAttempts
	if maxAttempts == 0 {
		maxAttempts = 3
	}

	opts := workflow.ActivityOptions{
		StartToCloseTimeout: activityStartToClose * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    time.Second,
			BackoffCoefficient: 2.0,
			MaximumInterval:    10 * time.Second,
			MaximumAttempts:    maxAttempts,
		},

		// ScheduleToStartTimeout is deliberately NOT set.
		//
		// Setting it truncates temporal_activity_schedule_to_start_latency at
		// the timeout value, so a queue that is badly backed up reports a
		// flat line instead of a rising one. Any real backlog investigation
		// should start by confirming this option is unset — see the Activity
		// Schedule-to-Start panel notes in the guide.
	}
	ctx = workflow.WithActivityOptions(ctx, opts)

	logger := workflow.GetLogger(ctx)
	logger.Info("order started", "orderID", in.OrderID)

	// CHAOS: non-determinism injection. Lab use only.
	//
	// Set NDE_INJECT=1 on the Worker and restart it while Workflows are still
	// open. This inserts a command that is absent from their recorded history,
	// so replay diverges and the Workflow Task fails with a non-determinism
	// error — the one failure mode Temporal cannot retry its way out of.
	//
	// This exists because the NDE alert asserts a specific label VALUE, and a
	// label name copied from a slide is a guess until a real NDE has been
	// observed on this SDK. `make chaos-nde` produces one on demand.
	if os.Getenv("NDE_INJECT") == "1" {
		_ = workflow.Sleep(ctx, time.Millisecond)
	}

	var result string
	if err := workflow.ExecuteActivity(ctx, ValidateOrder, in).Get(ctx, &result); err != nil {
		return "", fmt.Errorf("validation failed: %w", err)
	}
	if err := workflow.ExecuteActivity(ctx, ChargePayment, in).Get(ctx, &result); err != nil {
		return "", fmt.Errorf("payment failed: %w", err)
	}
	if err := workflow.ExecuteActivity(ctx, ShipOrder, in).Get(ctx, &result); err != nil {
		return "", fmt.Errorf("shipping failed: %w", err)
	}

	return "completed", nil
}

// ValidateOrder is fast and always succeeds. It exists so there is a healthy
// baseline Activity on the dashboard to contrast against ChargePayment.
func ValidateOrder(ctx context.Context, in OrderInput) (string, error) {
	return "validated", nil
}

// ChargePayment carries both chaos levers: artificial latency and injected
// failure.
func ChargePayment(ctx context.Context, in OrderInput) (string, error) {
	if in.ActivityDelayMs > 0 {
		select {
		case <-time.After(time.Duration(in.ActivityDelayMs) * time.Millisecond):
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}

	if in.FailureRate > 0 && rand.Float64() < in.FailureRate {
		return "", fmt.Errorf("payment declined for order %s", in.OrderID)
	}

	return "charged", nil
}

// ShipOrder is fast and always succeeds.
func ShipOrder(ctx context.Context, in OrderInput) (string, error) {
	return "shipped", nil
}
