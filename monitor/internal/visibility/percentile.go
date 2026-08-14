package visibility

import (
	"context"
	"fmt"
	"time"
)

// Counter is the one thing this package needs from a Temporal client:
// CountWorkflowExecutions. Narrow on purpose — it keeps the percentile search
// unit-testable without a server, and there is no reason for this code to be
// able to do anything else.
type Counter interface {
	Count(ctx context.Context, query string) (int64, error)
}

// PercentileResult is one derived percentile plus the evidence behind it.
type PercentileResult struct {
	P        float64
	Duration time.Duration
	Queries  int  // count calls spent finding it
	Exact    bool // false when the search hit its iteration cap
}

// DurationPercentiles derives percentiles of ExecutionDuration by BINARY
// SEARCH over CountWorkflowExecutions.
//
// The obvious implementation pages ListWorkflowExecutions and sorts. That is
// exact and completely impractical: a busy namespace closes millions of
// executions in 30 days and paging them to compute one number is both slow and
// rude to a rate-limited API.
//
// Instead: to find p99, search for the duration D where
//
//	count(closed AND ExecutionDuration <= D) / total ≈ 0.99
//
// The count is monotonically non-decreasing in D, so bisection converges in
// ~log2(range/tolerance) queries — around 25 for nanosecond-resolution search
// across a 24-hour range. Same API the monitor uses, and cheap enough to be
// polite.
//
// The result is approximate to `tolerance`. That is fine: the output is a
// proposed budget of 3× p99 that a human reviews before it becomes a threshold.
func DurationPercentiles(
	ctx context.Context,
	c Counter,
	workflowType string,
	lookback time.Duration,
	now time.Time,
	percentiles []float64,
	maxDuration time.Duration,
	tolerance time.Duration,
) (total int64, results []PercentileResult, err error) {

	total, err = c.Count(ctx, ClosedTotal(workflowType, lookback, now))
	if err != nil {
		return 0, nil, fmt.Errorf("counting closed executions: %w", err)
	}
	if total == 0 {
		return 0, nil, nil
	}

	for _, p := range percentiles {
		// Rank of the target execution, 1-based. ceil(p * total) so p99 of 100
		// executions is the 99th, not the 98th.
		target := int64(float64(total)*p + 0.999999)
		if target < 1 {
			target = 1
		}
		if target > total {
			target = total
		}

		lo, hi := time.Duration(0), maxDuration
		queries := 0
		exact := false

		for hi-lo > tolerance {
			// Guard against a pathological range producing thousands of calls.
			if queries >= 40 {
				break
			}
			mid := lo + (hi-lo)/2
			n, cerr := c.Count(ctx, ClosedDurationAtMost(workflowType, mid, lookback, now))
			queries++
			if cerr != nil {
				return total, nil, fmt.Errorf("percentile %.4f search: %w", p, cerr)
			}
			if n >= target {
				hi = mid // enough executions at or below mid; the answer is <= mid
			} else {
				lo = mid // not enough; the answer is above mid
			}
			if hi-lo <= tolerance {
				exact = true
			}
		}

		results = append(results, PercentileResult{
			P:        p,
			Duration: hi,
			Queries:  queries,
			Exact:    exact,
		})
	}
	return total, results, nil
}
