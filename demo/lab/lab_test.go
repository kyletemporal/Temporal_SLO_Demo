package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// The diagnosis cases below use the numbers MEASURED on the live lab (see
// README.md, "What the presets actually showed"), so a change to the
// heuristics that would have misread a real run fails here.

func lane(sdk string, cfg LaneConfig, d Derived, ds ...int) *LaneSnapshot {
	s := &LaneSnapshot{Config: cfg, Derived: d,
		Workers: []WorkerView{{WorkerReport: WorkerReport{SDK: sdk}}}}
	if len(ds) == 3 {
		s.Downstream.Limit, s.Downstream.InFlight, s.Downstream.Waiting = ds[0], ds[1], ds[2]
	}
	s.Derived.CacheHitRatio = d.CacheHitRatio
	return s
}

func expect(t *testing.T, got []string, want string) {
	t.Helper()
	if len(got) == 0 || !strings.Contains(got[0], want) {
		t.Fatalf("want first diagnosis to contain %q, got %q", want, got)
	}
}

func TestDiagnoseMeasuredCases(t *testing.T) {
	on := baseline
	cases := []struct {
		name string
		snap *LaneSnapshot
		want string
	}{
		{"slot-constrained (go)", lane("go 1.47.0", on, Derived{
			ActSlotsUsed: 1, ActSlotsTotal: 1, ActS2sP95: 37.7, ActQueueEst: 162,
			CPUCores: 0.02, CPULimit: 2, CacheHitRatio: 1}), "slot-bound"},
		{"poller-constrained (go)", lane("go 1.47.0", on, Derived{
			ActSlotsUsed: 17, ActSlotsTotal: 50, ActS2sP95: 0.5, ActQueueEst: 35,
			PollersAct: 1, CPUCores: 0.16, CPULimit: 2, CacheHitRatio: 1}), "poller-bound"},
		// CPU starves the pollers too: slots look free, and it must NOT be
		// called poller-bound.
		{"cpu-constrained (go)", lane("go 1.47.0", on, Derived{
			ActSlotsUsed: 7, ActSlotsTotal: 20, ActS2sP95: 3.1, WftS2sP95: 28,
			ActQueueEst: 12, WftQueueEst: 55, CPUCores: 1.0, CPULimit: 1, CacheHitRatio: 1}), "CPU-bound"},
		{"io-constrained (go)", lane("go 1.47.0", LaneConfig{Enabled: true, Rate: 3, Profile: "io"}, Derived{
			ActSlotsUsed: 20, ActSlotsTotal: 20, ActS2sP95: 6.9, ActQueueEst: 45,
			CPUCores: 0.03, CPULimit: 2, CacheHitRatio: 1}, 2, 2, 17), "downstream-bound"},
		{"cpu-constrained (python, GIL)", lane("python 1.34.0", on, Derived{
			WftSlotsUsed: 10, WftSlotsTotal: 10, WftS2sP95: 10, WftQueueEst: 69,
			ActSlotsUsed: 6, ActSlotsTotal: 20, CPUCores: 1.02, CPULimit: 1, CacheHitRatio: 1}), "GIL-bound"},
		// Measured: cache 1000, 20 Workflow threads, ~40 open. The cache held
		// 19, 58% of tasks missed, forced thread evictions only 0.4/s.
		{"java-thread-starved", lane("java 1.28.4", func() LaneConfig { c := on; c.CacheSize = 1000; c.WorkflowThreads = 20; return c }(), Derived{
			ActSlotsUsed: 40, ActSlotsTotal: 50, ActS2sP95: 0.004, WftS2sP95: 0.011,
			CacheSize: 19, CacheHitRatio: 0.42, ThreadEvictPerSec: 0.36, CPUCores: 0.02, CPULimit: 2}), "Workflow threads cap the sticky cache"},
		// The same hit ratio on Go is plain cache thrash: no thread pool there.
		{"cache thrash is not thread-capped on go", lane("go 1.47.0", func() LaneConfig { c := on; c.CacheSize = 2; c.WorkflowThreads = 1000; return c }(), Derived{
			CacheSize: 1, CacheHitRatio: 0.0, ActS2sP95: 0.01, CPUCores: 0.05, CPULimit: 2}), "Sticky cache hit ratio"},
		// Measured: target 60%, system CPU 0.586, 1-2 of 50 Activity slots
		// granted, schedule-to-start 23.6s and climbing. Process CPU 1.2 of 2
		// is below the CPU-bound threshold, and slots are NOT "all busy".
		{"java-resource-based", lane("java 1.28.4", func() LaneConfig { c := on; c.TuningMode = "resource"; c.TargetCPUPct = 60; c.ActSlots = 50; return c }(), Derived{
			ActSlotsUsed: 1, ActSlotsTotal: 50, ActS2sP95: 23.6, ActQueueEst: 312, TunerManaged: true, TunerCPU: 0.586,
			CPUCores: 1.22, CPULimit: 2, CacheHitRatio: 1}), "Resource-based tuner is holding Activity slots"},
		{"baseline", lane("go 1.47.0", on, Derived{
			ActSlotsUsed: 4, ActSlotsTotal: 10, ActS2sP95: 0.008, CPUCores: 0.05, CPULimit: 2, CacheHitRatio: 1}), "Keeping up"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) { expect(t, diagnose(c.snap), c.want) })
	}

	healthy := ServerHealth{PersistenceP95: 0.0046, RespondP95: 0.0464}
	withServer := func(s *LaneSnapshot, h ServerHealth) *LaneSnapshot { s.Server = h; return s }
	serverCases := []struct {
		name string
		snap *LaneSnapshot
		want string
	}{
		// Postgres capped at 0.3 CPU, 16 pollers per type, Worker idle.
		{"server-bound (postgres capped)", withServer(lane("go 1.47.0", on, Derived{
			ActSlotsUsed: 26, ActSlotsTotal: 60, ActS2sP95: 55.5, ActQueueEst: 828,
			WftSlotsUsed: 9, WftSlotsTotal: 50, WftS2sP95: 65.3, WftQueueEst: 102,
			PollActP50: 0.903, PollWftP50: 1.0, CPUCores: 0.05, CPULimit: 2, CacheHitRatio: 1}),
			ServerHealth{PersistenceP95: 0.0963, RespondP95: 4.37}), "server-bound"},
		// The heavy-load run that LOOKED server-bound (server at 240% CPU) and
		// was not: raising pollers 4 -> 16 cut schedule-to-start 4.4s -> 84ms.
		// Fast polls with healthy server metrics must stay poller-bound.
		{"poller-bound under heavy server load", withServer(lane("go 1.47.0", on, Derived{
			ActSlotsUsed: 0, ActSlotsTotal: 60, ActS2sP95: 4.4, ActQueueEst: 115,
			PollersAct: 4, PollActP50: 0.011, CPUCores: 0.2, CPULimit: 2, CacheHitRatio: 1}), healthy), "poller-bound"},
		// A slow server holds slots until completion RPCs return; full slots
		// must not be blamed on the slot count.
		{"server-bound with full slots", withServer(lane("go 1.47.0", on, Derived{
			ActSlotsUsed: 60, ActSlotsTotal: 60, ActS2sP95: 20, ActQueueEst: 300,
			PollActP50: 0.002, CPUCores: 0.05, CPULimit: 2, CacheHitRatio: 1}),
			ServerHealth{PersistenceP95: 0.09, RespondP95: 3.0}), "server-bound"},
		// Without server metrics, fall back to the Worker-side checks.
		{"server metrics unavailable", withServer(lane("go 1.47.0", on, Derived{
			ActSlotsUsed: 1, ActSlotsTotal: 1, ActS2sP95: 37.7, ActQueueEst: 162,
			CPUCores: 0.02, CPULimit: 2, CacheHitRatio: 1}), ServerHealth{Err: "connection refused"}), "slot-bound"},
	}
	for _, c := range serverCases {
		t.Run(c.name, func(t *testing.T) { expect(t, diagnose(c.snap), c.want) })
	}

	cache := diagnose(lane("go 1.47.0", on, Derived{CacheHitRatio: 0, CPULimit: 2, ReplayP95: 0.0004}))
	if !strings.Contains(strings.Join(cache, " "), "Sticky cache hit ratio 0%") {
		t.Fatalf("cache thrash not reported: %q", cache)
	}
}

func TestGaugeKeys(t *testing.T) {
	for _, c := range []struct {
		name string
		tags map[string]string
		want string
	}{
		{"worker_task_slots_used", map[string]string{"worker_type": "ActivityWorker"}, "act_slots_used"},
		{"worker_task_slots_available", map[string]string{"worker_type": "WorkflowWorker"}, "wft_slots_available"},
		{"worker_task_slots_used", map[string]string{"worker_type": "LocalActivityWorker"}, ""},
		{"num_pollers", map[string]string{"poller_type": "workflow_sticky_task"}, "pollers_sticky"},
		{"sticky_cache_size", nil, "cache_size"},
	} {
		if got := gaugeKey(c.name, c.tags); got != c.want {
			t.Errorf("gaugeKey(%s, %v) = %q, want %q", c.name, c.tags, got, c.want)
		}
	}
	if canonical("temporal_num_pollers") != "num_pollers" {
		t.Fatal("prefix not stripped")
	}
}

// While a swap overlaps two Workers, gauges sum across them; once the old one
// drains, its values must stop counting.
func TestAggregatorSwapAndPercentiles(t *testing.T) {
	a := newAggregator()
	a.setGauge("w#1", "act_slots_used", 3)
	a.setGauge("w#2", "act_slots_used", 2)
	if _, g, _ := a.snapshot(); g["act_slots_used"] != 5 {
		t.Fatalf("overlap sum = %v, want 5", g["act_slots_used"])
	}
	a.dropInstance("w#1")
	if _, g, _ := a.snapshot(); g["act_slots_used"] != 2 {
		t.Fatalf("after drain = %v, want 2", g["act_slots_used"])
	}

	for i := 1; i <= 100; i++ {
		a.record("act_s2s", time.Duration(i)*time.Millisecond)
	}
	c, _, lat := a.snapshot()
	p := lat["act_s2s"]
	if p.N != 100 || p.P95 < 0.094 || p.P95 > 0.096 || p.Mean < 0.0504 || p.Mean > 0.0506 {
		t.Fatalf("percentiles wrong: %+v", p)
	}
	a.record("act_exec", time.Millisecond)
	if c, _, _ = a.snapshot(); c["act_completed"] != 1 {
		t.Fatalf("timer count not mirrored to act_completed: %v", c)
	}
}

// Mutating endpoints must refuse anything a cross-site page can send without
// a CORS preflight: GET, and POST with a form or text Content-Type.
func TestMutatingGuard(t *testing.T) {
	called := false
	h := mutating(func(w http.ResponseWriter, r *http.Request) { called = true })
	for _, c := range []struct {
		method, ct string
		want       int
	}{
		{http.MethodGet, "", http.StatusMethodNotAllowed},
		{http.MethodPost, "application/x-www-form-urlencoded", http.StatusUnsupportedMediaType},
		{http.MethodPost, "text/plain", http.StatusUnsupportedMediaType},
		{http.MethodPost, "application/json", http.StatusOK},
	} {
		called = false
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(c.method, "/api/burst?lane=go&n=2000", nil)
		if c.ct != "" {
			req.Header.Set("Content-Type", c.ct)
		}
		h(rec, req)
		if rec.Code != c.want || called != (c.want == http.StatusOK) {
			t.Errorf("%s %q: code %d called %v, want %d", c.method, c.ct, rec.Code, called, c.want)
		}
	}
}

// The idle auto-pause exists because a forgotten lane filled the disk (see
// autoPause). Pin the behaviour that makes it safe to leave on during demos.
func TestAutoPauseIdleLanes(t *testing.T) {
	t0 := time.Now()
	newC := func(limit time.Duration) *controller {
		c := &controller{lanes: map[string]*laneState{}, idlePause: limit}
		for _, l := range lanes {
			cfg := baseline
			cfg.Enabled = l == "java" // only java is generating load
			cfg.Rate = 60
			c.lanes[l] = &laneState{cfg: cfg, touchedAt: t0, ds: newLimiter(cfg.IOConcurrency)}
		}
		return c
	}

	c := newC(30 * time.Minute)
	if got := c.autoPause(t0.Add(29 * time.Minute)); len(got) != 0 {
		t.Fatalf("paused before the limit: %v", got)
	}
	if s := c.laneSnapshot("java", c.lanes["java"]); s.PauseIn < 50 || s.PauseIn > 31*60 {
		t.Fatalf("countdown not exposed: pauseInSeconds=%v", s.PauseIn)
	}

	got := c.autoPause(t0.Add(30 * time.Minute))
	if len(got) != 1 || got[0] != "java" {
		t.Fatalf("want only the running java lane paused, got %v", got)
	}
	js := c.lanes["java"]
	if js.cfg.Enabled || js.cfg.Rate != 60 {
		t.Fatalf("pause must flip Enabled only and keep settings: %+v", js.cfg)
	}
	s := c.laneSnapshot("java", js)
	if !strings.Contains(s.AutoPaused, "auto-paused") || s.PauseIn != 0 {
		t.Fatalf("snapshot must explain the pause: %q, pauseIn=%v", s.AutoPaused, s.PauseIn)
	}

	// A person resuming restarts the clock and clears the banner.
	cfg := js.cfg
	cfg.Enabled = true
	c.setConfig(js, cfg)
	if js.autoPaused != "" {
		t.Fatalf("banner survived a resume: %q", js.autoPaused)
	}
	if got := c.autoPause(time.Now().Add(29 * time.Minute)); len(got) != 0 {
		t.Fatalf("clock was not restarted by the change: %v", got)
	}

	// 0 disables it.
	if got := newC(0).autoPause(t0.Add(1000 * time.Hour)); len(got) != 0 {
		t.Fatalf("LAB_IDLE_PAUSE_MIN=0 must disable auto-pause, got %v", got)
	}
}
