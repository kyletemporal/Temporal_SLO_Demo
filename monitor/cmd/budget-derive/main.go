// Command budget-derive proposes per-workflow-type SLO budgets from observed
// history.
//
// It enumerates workflow types active in a namespace, derives percentiles of
// ExecutionDuration over a lookback window, and emits a starter slo-config.yaml
// with budgets at 3x observed p99.
//
// DERIVED BUDGETS ARE NOT ALERT THRESHOLDS. Every one is written with a TODO
// and the raw percentiles retained as comments, because a number nobody can
// defend is worse than no number.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"
	"text/tabwriter"
	"time"

	"go.temporal.io/api/serviceerror"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"

	"github.com/temporal-slo-demo/monitor/internal/visibility"
)

// row is one workflow type's derived distribution.
type row struct {
	name    string
	total   int64
	results []visibility.PercentileResult
}

type countClient struct {
	c         client.Client
	namespace string
	calls     int
	pace      time.Duration
}

// Count paces itself and backs off on RESOURCE_EXHAUSTED.
//
// Deriving four percentiles for a handful of types is ~50 count queries. Fired
// back-to-back at a rate-limited Visibility API — hardest on Cloud — that is
// enough to get throttled, and a throttled derivation produces partial results
// rather than an error you would notice.
func (cc *countClient) Count(ctx context.Context, query string) (int64, error) {
	const maxRetries = 5
	backoff := time.Second

	for attempt := 0; ; attempt++ {
		if cc.calls > 0 && cc.pace > 0 {
			select {
			case <-time.After(cc.pace):
			case <-ctx.Done():
				return 0, ctx.Err()
			}
		}
		cc.calls++
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
		fmt.Fprintf(os.Stderr, "  rate limited, backing off %v (attempt %d/%d)\n", backoff, attempt+1, maxRetries)
		select {
		case <-time.After(backoff):
		case <-ctx.Done():
			return 0, ctx.Err()
		}
		backoff *= 2
	}
}

func main() {
	var (
		address   = flag.String("address", "localhost:7233", "Temporal frontend address")
		namespace = flag.String("namespace", "default", "Temporal namespace")
		lookback  = flag.Duration("lookback", 30*24*time.Hour, "how far back to sample closed executions")
		out       = flag.String("out", "slo-config.generated.yaml", "path to write the starter config")
		kind      = flag.String("deployment-kind", "self-hosted", "self-hosted | cloud")
		maxDur    = flag.Duration("max-duration", 24*time.Hour, "upper bound for the percentile search")
		tolerance = flag.Duration("tolerance", time.Second, "percentile search precision")
		multiple  = flag.Float64("budget-multiple", 3.0, "proposed budget as a multiple of observed p99")
		pace      = flag.Duration("pace", 200*time.Millisecond, "delay between Visibility count queries")
		typesCSV  = flag.String("types", "", "comma-separated workflow types; skips discovery (use on busy namespaces)")
	)
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()

	c, err := client.Dial(client.Options{HostPort: *address, Namespace: *namespace})
	if err != nil {
		fatal("connecting to Temporal at %s: %v", *address, err)
	}
	defer c.Close()

	cc := &countClient{c: c, namespace: *namespace, pace: *pace}
	now := time.Now().UTC()

	var types []string
	if *typesCSV != "" {
		for _, t := range strings.Split(*typesCSV, ",") {
			if t = strings.TrimSpace(t); t != "" {
				types = append(types, t)
			}
		}
	} else {
		var derr error
		types, derr = discoverTypes(ctx, c, *namespace, *lookback, now)
		if derr != nil {
			fatal("discovering workflow types: %v", derr)
		}
	}
	if len(types) == 0 {
		fatal("no closed workflow executions in the last %v — nothing to derive a budget from", *lookback)
	}

	percentiles := []float64{0.50, 0.95, 0.99, 0.999}
	var rows []row

	for _, wt := range types {
		total, res, err := visibility.DurationPercentiles(
			ctx, cc, wt, *lookback, now, percentiles, *maxDur, *tolerance)
		if err != nil {
			fatal("deriving percentiles for %s: %v", wt, err)
		}
		if total == 0 {
			fmt.Fprintf(os.Stderr, "skipping %s: no closed executions in window\n", wt)
			continue
		}
		rows = append(rows, row{name: wt, total: total, results: res})
	}
	if len(rows) == 0 {
		fatal("no workflow type had closed executions in the window")
	}

	// ---- summary table -----------------------------------------------------
	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "\nWORKFLOW TYPE\tCLOSED\tp50\tp95\tp99\tp99.9\tPROPOSED BUDGET")
	fmt.Fprintln(tw, "-------------\t------\t---\t---\t---\t-----\t---------------")
	suspect := 0
	for _, r := range rows {
		p := map[float64]time.Duration{}
		bad := map[float64]bool{}
		for _, x := range r.results {
			p[x.P] = x.Duration
			if x.AboveRange || !x.Converged {
				bad[x.P] = true
				suspect++
			}
		}
		budget := "-"
		if !bad[0.99] {
			budget = short(proposeBudget(p[0.99], *multiple))
		} else {
			budget = "UNRELIABLE"
		}
		fmt.Fprintf(tw, "%s\t%d\t%s\t%s\t%s\t%s\t%s\n",
			r.name, r.total,
			mark(p[0.50], bad[0.50]), mark(p[0.95], bad[0.95]),
			mark(p[0.99], bad[0.99]), mark(p[0.999], bad[0.999]), budget)
	}
	tw.Flush()

	if suspect > 0 {
		fmt.Println("\n! marks a percentile the search could not pin down — almost always")
		fmt.Println("  because the real value exceeds -max-duration (currently " + maxDur.String() + ").")
		fmt.Println("  Raise -max-duration and re-run. A budget is NOT proposed from one of these,")
		fmt.Println("  because a clamped percentile looks exactly like a measured one.")
	}

	lowN := false
	for _, r := range rows {
		if r.total < 100 {
			lowN = true
		}
	}
	fmt.Printf("\n%d type(s), %d Visibility count queries, lookback %v, search tolerance %v\n",
		len(rows), cc.calls, *lookback, *tolerance)
	if lowN {
		fmt.Println("\nWARNING: at least one type has fewer than 100 closed executions.")
		fmt.Println("         A p99 over a small sample is a single slow execution wearing a")
		fmt.Println("         statistic's hat. Treat these budgets as placeholders.")
	}

	// ---- generated config --------------------------------------------------
	if err := os.WriteFile(*out, []byte(render(rows, percentiles, *kind, *namespace, *address, *lookback, *multiple, now)), 0o644); err != nil {
		fatal("writing %s: %v", *out, err)
	}
	fmt.Printf("\nWrote %s\n", *out)
	fmt.Println("REVIEW BEFORE USE: every budget is marked TODO. Derived numbers must not")
	fmt.Println("become alert thresholds without a human agreeing to them.")
}

// discoverTypes finds workflow types with closed executions in the window.
//
// Uses GROUP BY on the count API rather than paging executions — same reason
// the percentile search does: we want counts, not pages.
func discoverTypes(ctx context.Context, c client.Client, ns string, lookback time.Duration, now time.Time) ([]string, error) {
	from := now.Add(-lookback).UTC().Truncate(time.Second)

	// GROUP BY WorkflowType would be the right tool and IS NOT AVAILABLE:
	// Temporal rejects it with "'group by' clause is only supported for
	// ExecutionStatus search attribute" (verified on 1.26.2). So discovery has
	// to page — the one place in this codebase where listing is acceptable,
	// because it is a one-shot step rather than a poll loop.
	//
	// Paging is bounded, which means discovery can be INCOMPLETE on a busy
	// namespace. That must be loud: silently missing a workflow type means it
	// silently has no SLO.
	seen := map[string]bool{}
	var token []byte
	truncated := false
	scanned := 0
	const maxPages = 20
	for i := 0; i < maxPages; i++ {
		lr, err := c.ListWorkflow(ctx, &workflowservice.ListWorkflowExecutionsRequest{
			Namespace:     ns,
			PageSize:      1000,
			NextPageToken: token,
			Query:         fmt.Sprintf("CloseTime >= '%s'", from.Format(time.RFC3339)),
		})
		if err != nil {
			return nil, err
		}
		for _, e := range lr.GetExecutions() {
			scanned++
			if t := e.GetType().GetName(); t != "" {
				seen[t] = true
			}
		}
		token = lr.GetNextPageToken()
		if len(token) == 0 {
			break
		}
		if i == maxPages-1 {
			truncated = true
		}
	}
	if truncated {
		fmt.Fprintf(os.Stderr,
			"\nWARNING: type discovery stopped after %d executions (%d pages).\n"+
				"         Types that appear only in older executions are MISSING from this\n"+
				"         run, and a missing type silently has no SLO. Pass -types to name\n"+
				"         them explicitly, or shorten -lookback.\n\n", scanned, maxPages)
	}
	out := make([]string, 0, len(seen))
	for t := range seen {
		out = append(out, t)
	}
	sort.Strings(out)
	return out, nil
}

// proposeBudget rounds up to something a human would actually write in a config
// file. A budget of 4h13m47s invites bikeshedding; 5h does not.
func proposeBudget(p99 time.Duration, multiple float64) time.Duration {
	raw := time.Duration(float64(p99) * multiple)
	switch {
	// Granularity has to stay proportional to the value. Rounding a 2.4s
	// budget up to the nearest 10s makes it a 12x-p99 budget, not the 3x that
	// was asked for — the rounding silently rewrites the proposal.
	case raw < 10*time.Second:
		return roundUp(raw, 100*time.Millisecond)
	case raw < time.Minute:
		return roundUp(raw, time.Second)
	case raw < time.Hour:
		return roundUp(raw, time.Minute)
	case raw < 24*time.Hour:
		return roundUp(raw, 15*time.Minute)
	default:
		return roundUp(raw, time.Hour)
	}
}

func roundUp(d, to time.Duration) time.Duration {
	if d%to == 0 {
		return d
	}
	return (d/to + 1) * to
}

// mark flags a percentile the search could not trust, so it cannot be mistaken
// for a measurement in the summary table.
func mark(d time.Duration, bad bool) string {
	if bad {
		return short(d) + "!"
	}
	return short(d)
}

func short(d time.Duration) string {
	switch {
	case d == 0:
		return "-"
	case d < time.Second:
		return d.Round(time.Millisecond).String()
	case d < time.Minute:
		return d.Round(100 * time.Millisecond).String()
	default:
		return d.Round(time.Second).String()
	}
}

func render(rows []row, percentiles []float64, kind, ns, addr string, lookback time.Duration, multiple float64, now time.Time) string {

	var b strings.Builder
	fmt.Fprintf(&b, `# GENERATED by budget-derive on %s
#
# Budgets below are proposals at %.1fx observed p99 over the last %v.
# THEY ARE NOT REVIEWED AND MUST NOT BE TREATED AS AGREED THRESHOLDS.
#
# A p99 describes what the system HAS done, not what users NEED. Those are
# different questions, and only the second one belongs in an SLO. Use these as
# a starting point for that conversation.

deployment:
  kind: %s
  namespace: %s
  address: %s
  stuck_detection: auto      # probes TemporalReportedProblems at startup

defaults:
  poll_interval: 60s
  window_poll_interval: 600s
  buckets: [1, 2, 5]
  slo_window: 28d
  # Terminal states counted as closed. Terminated and Canceled are included on
  # purpose: if terminating a late workflow removed it from the denominator,
  # the SLO would improve as we destroyed the evidence.
  closed_over_budget_statuses: [Completed, Failed, Canceled, Terminated, TimedOut]

workflow_types:
`, now.Format(time.RFC3339), multiple, lookback, kind, ns, addr)

	for _, r := range rows {
		p := map[float64]time.Duration{}
		for _, x := range r.results {
			p[x.P] = x.Duration
		}
		unreliable := false
		for _, x := range r.results {
			if x.P == 0.99 && (x.AboveRange || !x.Converged) {
				unreliable = true
			}
		}
		budget := proposeBudget(p[0.99], multiple)

		fmt.Fprintf(&b, "  - name: %s\n", r.name)
		fmt.Fprintf(&b, "    task_queue: \"\"            # TODO: set if this type is confined to one queue\n")
		fmt.Fprintf(&b, "    # observed over %v from %d closed executions:\n", lookback, r.total)
		fmt.Fprintf(&b, "    #   p50   %s\n", short(p[0.50]))
		fmt.Fprintf(&b, "    #   p95   %s\n", short(p[0.95]))
		fmt.Fprintf(&b, "    #   p99   %s\n", short(p[0.99]))
		fmt.Fprintf(&b, "    #   p99.9 %s\n", short(p[0.999]))
		if r.total < 100 {
			fmt.Fprintf(&b, "    # WARNING: only %d executions — this p99 is one slow run, not a distribution\n", r.total)
		}
		if unreliable {
			fmt.Fprintf(&b, "    # !! p99 could not be determined: it exceeds the search range.\n")
			fmt.Fprintf(&b, "    # !! Re-run budget-derive with a larger -max-duration. The value below\n")
			fmt.Fprintf(&b, "    # !! is a LOWER BOUND, not a measurement, and will not load as-is.\n")
			fmt.Fprintf(&b, "    budget: REPLACE_ME_p99_EXCEEDED_SEARCH_RANGE\n")
		} else {
			fmt.Fprintf(&b, "    budget: %s              # TODO: %.1fx p99, derived not agreed — review\n", budget, multiple)
		}
		fmt.Fprintf(&b, "    objective: 0.99           # TODO: what do users actually need?\n")
		fmt.Fprintf(&b, "    owner: TODO-team          # TODO: routes the page\n")
		fmt.Fprintf(&b, "    phase_attribute: null\n\n")
	}
	return b.String()
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "error: "+format+"\n", args...)
	os.Exit(1)
}
