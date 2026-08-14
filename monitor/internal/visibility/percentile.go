package visibility

import (
	"context"
	"fmt"
	"math"
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
//
// Converged and AboveRange exist so a caller can tell a trustworthy number from
// a clamped one. Without them, a percentile that fell outside the search range
// comes back looking like an ordinary answer and silently becomes a budget.
type PercentileResult struct {
	P        float64
	Duration time.Duration
	Queries  int
	// Converged is false when the search hit its iteration cap before reaching
	// tolerance. The value is a bound, not an answer.
	Converged bool
	// AboveRange is true when the real percentile is LARGER than maxDuration.
	// Duration is then only a lower bound and must not be used as a budget.
	AboveRange bool
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
		// Rank of the target execution, 1-based, rounded UP: p99 of 100
		// executions is the 99th, not the 98th. math.Ceil rather than an
		// epsilon fudge — float64 cannot represent 0.99 exactly, and
		// `total*p + 0.999999` lands on the wrong side of the boundary for
		// some totals.
		target := int64(math.Ceil(float64(total) * p))
		if target < 1 {
			target = 1
		}
		if target > total {
			target = total
		}

		// Does the answer even lie inside the search range? Without this
		// check, a percentile above maxDuration converges silently to
		// maxDuration and is indistinguishable from a real measurement.
		atMax, cerr := c.Count(ctx, ClosedDurationAtMost(workflowType, maxDuration, lookback, now))
		queries := 1
		if cerr != nil {
			return total, nil, fmt.Errorf("percentile %.4f range probe: %w", p, cerr)
		}
		if atMax < target {
			results = append(results, PercentileResult{
				P: p, Duration: maxDuration, Queries: queries,
				Converged: false, AboveRange: true,
			})
			continue
		}

		lo, hi := time.Duration(0), maxDuration
		converged := false

		for hi-lo > tolerance {
			if queries >= 40 {
				break // pathological range; report as unconverged rather than guessing
			}
			mid := lo + (hi-lo)/2
			n, cerr := c.Count(ctx, ClosedDurationAtMost(workflowType, mid, lookback, now))
			queries++
			if cerr != nil {
				return total, nil, fmt.Errorf("percentile %.4f search: %w", p, cerr)
			}
			if n >= target {
				hi = mid
			} else {
				lo = mid
			}
		}
		converged = hi-lo <= tolerance

		results = append(results, PercentileResult{
			P: p, Duration: hi, Queries: queries, Converged: converged,
		})
	}
	return total, results, nil
}
