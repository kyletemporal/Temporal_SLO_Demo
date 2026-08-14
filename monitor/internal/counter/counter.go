// Package counter wraps Temporal's CountWorkflowExecutions with pacing and
// backoff.
//
// Every number this service publishes comes from a count query, so the
// Visibility API is the single dependency that can take the monitor down — or,
// worse, that the monitor can take down. Both loops share one paced client
// rather than querying independently.
package counter

import (
	"context"
	"errors"
	"time"

	"go.temporal.io/api/serviceerror"
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

// Count paces itself and backs off on RESOURCE_EXHAUSTED.
//
// Pacing is a correctness requirement, not politeness. A monitor that gets
// itself rate limited degrades the Visibility API for the application it exists
// to watch, and a throttled poll produces a STALE GAUGE rather than an error
// anyone notices. Cloud enforces per-namespace limits, so this matters most
// exactly where the monitor is most useful.
func (cc *Client) Count(ctx context.Context, query string) (int64, error) {
	const maxRetries = 4
	backoff := time.Second

	for attempt := 0; ; attempt++ {
		if cc.pace > 0 {
			select {
			case <-time.After(cc.pace):
			case <-ctx.Done():
				return 0, ctx.Err()
			}
		}
		resp, err := cc.c.CountWorkflow(ctx, &workflowservice.CountWorkflowExecutionsRequest{
			Namespace: cc.namespace,
			Query:     query,
		})
		if err == nil {
			return resp.GetCount(), nil
		}
		var re *serviceerror.ResourceExhausted
		if !errors.As(err, &re) || attempt >= maxRetries {
			return 0, err
		}
		select {
		case <-time.After(backoff):
		case <-ctx.Done():
			return 0, ctx.Err()
		}
		backoff *= 2
	}
}
