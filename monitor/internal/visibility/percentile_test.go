package visibility

import (
	"context"
	"sort"
	"testing"
	"time"
)

// fakeCounter answers counts from a known duration distribution, so the binary
// search can be verified against an exact answer computed directly.
type fakeCounter struct {
	durations []time.Duration
	calls     int
}

func (f *fakeCounter) Count(_ context.Context, query string) (int64, error) {
	f.calls++
	// ClosedTotal has no ExecutionDuration clause; ClosedDurationAtMost does.
	bound, ok := parseDurationBound(query)
	if !ok {
		return int64(len(f.durations)), nil
	}
	var n int64
	for _, d := range f.durations {
		if d <= bound {
			n++
		}
	}
	return n, nil
}

// parseDurationBound pulls N out of "ExecutionDuration <= N" if present.
func parseDurationBound(q string) (time.Duration, bool) {
	const marker = "ExecutionDuration <= "
	i := indexOf(q, marker)
	if i < 0 {
		return 0, false
	}
	rest := q[i+len(marker):]
	var n int64
	for _, ch := range rest {
		if ch < '0' || ch > '9' {
			break
		}
		n = n*10 + int64(ch-'0')
	}
	return time.Duration(n), true
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func exactPercentile(ds []time.Duration, p float64) time.Duration {
	s := append([]time.Duration(nil), ds...)
	sort.Slice(s, func(i, j int) bool { return s[i] < s[j] })
	rank := int(float64(len(s))*p + 0.999999)
	if rank < 1 {
		rank = 1
	}
	if rank > len(s) {
		rank = len(s)
	}
	return s[rank-1]
}

func TestDurationPercentiles_MatchesExactWithinTolerance(t *testing.T) {
	// 1000 executions, mostly fast with a deliberate slow tail — the shape a
	// real workflow type has, and the shape that makes p99 differ sharply
	// from p50.
	var ds []time.Duration
	for i := 0; i < 950; i++ {
		ds = append(ds, time.Duration(100+i)*time.Millisecond)
	}
	for i := 0; i < 50; i++ {
		ds = append(ds, time.Duration(30+i)*time.Second)
	}

	f := &fakeCounter{durations: ds}
	tol := 50 * time.Millisecond

	total, res, err := DurationPercentiles(context.Background(), f, "T",
		30*24*time.Hour, time.Now(),
		[]float64{0.50, 0.95, 0.99, 0.999},
		2*time.Minute, tol)
	if err != nil {
		t.Fatal(err)
	}
	if total != 1000 {
		t.Fatalf("total = %d, want 1000", total)
	}

	for _, r := range res {
		want := exactPercentile(ds, r.P)
		diff := r.Duration - want
		if diff < 0 {
			diff = -diff
		}
		// Bisection lands within one tolerance step of the true value.
		if diff > tol {
			t.Errorf("p%.3f = %v, exact %v, off by %v (tolerance %v)",
				r.P, r.Duration, want, diff, tol)
		}
		if !r.Exact {
			t.Errorf("p%.3f did not converge in %d queries", r.P, r.Queries)
		}
	}
}

func TestDurationPercentiles_QueryCountIsLogarithmic(t *testing.T) {
	// The reason for binary search at all: cost must not scale with the number
	// of executions. 100k executions must cost the same as 1k.
	var small, large []time.Duration
	for i := 0; i < 1_000; i++ {
		small = append(small, time.Duration(i)*time.Millisecond)
	}
	for i := 0; i < 100_000; i++ {
		large = append(large, time.Duration(i%1000)*time.Millisecond)
	}

	run := func(ds []time.Duration) int {
		f := &fakeCounter{durations: ds}
		_, _, err := DurationPercentiles(context.Background(), f, "T",
			30*24*time.Hour, time.Now(), []float64{0.99},
			2*time.Minute, 10*time.Millisecond)
		if err != nil {
			t.Fatal(err)
		}
		return f.calls
	}

	a, b := run(small), run(large)
	if a != b {
		t.Errorf("query count varied with dataset size: %d vs %d", a, b)
	}
	if a > 30 {
		t.Errorf("percentile search used %d queries; expected ~log2(range/tolerance)", a)
	}
}

func TestDurationPercentiles_NoClosedExecutions(t *testing.T) {
	// A workflow type with no closed executions must yield nothing derivable
	// rather than a confident zero budget.
	f := &fakeCounter{}
	total, res, err := DurationPercentiles(context.Background(), f, "T",
		30*24*time.Hour, time.Now(), []float64{0.99}, time.Minute, time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	if total != 0 || res != nil {
		t.Errorf("want no results for empty type, got total=%d res=%v", total, res)
	}
}

func TestDurationPercentiles_RankRoundsUp(t *testing.T) {
	// p99 of 100 executions is the 99th, not the 98th. Rounding down here
	// makes every derived budget slightly too generous.
	ds := make([]time.Duration, 100)
	for i := range ds {
		ds[i] = time.Duration(i+1) * time.Second
	}
	f := &fakeCounter{durations: ds}
	_, res, err := DurationPercentiles(context.Background(), f, "T",
		30*24*time.Hour, time.Now(), []float64{0.99},
		200*time.Second, 100*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	got := res[0].Duration.Round(time.Second)
	if got != 99*time.Second {
		t.Errorf("p99 = %v, want 99s (rank must round up)", got)
	}
}
