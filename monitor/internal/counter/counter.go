// Package counter wraps Temporal's CountWorkflowExecutions with pacing and a
// per-query deadline.
//
// Every number this service publishes comes from a count query, so the
// Visibility API is the single dependency that can take the monitor down — or,
// worse, that the monitor can take down. Both loops share one paced client
// rather than querying independently.
package counter

import (
	"context"
	"time"

	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
)

// Counter is the only operation the monitor needs. It is an interface so the
// poll loops can be tested without a Temporal server.
type Counter interface {
	Count(ctx context.Context, query string) (int64, error)
}

type Client struct {
	c         client.Client
	namespace string
	pace      time.Duration
}

func New(c client.Client, namespace string, pace time.Duration) *Client {
	return &Client{c: c, namespace: namespace, pace: pace}
}

// queryTimeout bounds one Count, retries included. Both poll loops share one
// goroutine, so a query that hangs starves the other loop.
const queryTimeout = 20 * time.Second

// Count paces itself and runs the query under queryTimeout.
//
// Pacing is a correctness requirement, not politeness. A monitor that gets
// itself rate limited degrades the Visibility API for the application it exists
// to watch, and a throttled poll produces a STALE GAUGE rather than an error
// anyone notices. Cloud enforces per-namespace limits, so this matters most
// exactly where the monitor is most useful.
//
// There is deliberately NO retry loop here. The Go SDK's gRPC interceptor
// already retries ResourceExhausted and Unavailable (sdk v1.47
// internal/common/retry/interceptor.go). A second layer of 4 attempts with
// doubling backoff stacked on top of it: one throttled query could take ~65s
// and hit the rate limiter harder, while starving the other poll loop. A query
// that still fails returns its error, the gauge keeps its last value, and
// freshness goes stale — which is what MonitorPollsStale exists to report.
func (cc *Client) Count(ctx context.Context, query string) (int64, error) {
	if cc.pace > 0 {
		select {
		case <-time.After(cc.pace):
		case <-ctx.Done():
			return 0, ctx.Err()
		}
	}
	ctx, cancel := context.WithTimeout(ctx, queryTimeout)
	defer cancel()
	resp, err := cc.c.CountWorkflow(ctx, &workflowservice.CountWorkflowExecutionsRequest{
		Namespace: cc.namespace,
		Query:     query,
	})
	if err != nil {
		return 0, err
	}
	return resp.GetCount(), nil
}
