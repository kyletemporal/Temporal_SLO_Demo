package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log"
	"math/rand/v2"
	"net/http"
	"os"
	"runtime"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	sdktally "go.temporal.io/sdk/contrib/tally"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

// httpClient talks to the controller. A timeout, so a hung controller stalls
// one report rather than the reporting loop forever.
var httpClient = &http.Client{Timeout: 5 * time.Second}

// exitCacheResize is the exit code for "restart me with a new sticky cache
// size". Compose restarts the container; the UI shows the gap.
const exitCacheResize = 3

type goWorker struct {
	lane       string
	namespace  string
	controller string
	base       client.Client
	prom       client.MetricsHandler
	agg        *aggregator
	startedAt  time.Time
	id         string

	cfg     atomic.Pointer[LaneConfigVersioned]
	applied atomic.Int64

	mu         sync.Mutex
	current    worker.Worker
	currentCl  client.Client
	currentID  string
	running    *LaneConfig // options the CURRENT Worker was built with
	cacheSize  int
	swapCount  int
	itersPerMs float64
}

func runWorker() {
	lane := env("LANE", "go")
	g := &goWorker{
		lane:       lane,
		namespace:  env("TEMPORAL_NAMESPACE", labNamespace),
		controller: env("CONTROLLER_URL", "http://lab-controller:8095"),
		agg:        newAggregator(),
		startedAt:  time.Now(),
	}
	host, _ := os.Hostname()
	g.id = fmt.Sprintf("%s-%s", lane, host)

	// Prometheus on :9464 as well as the live view, so the lab Workers also
	// show up on the existing Grafana boards — under task_queue="lab_go":
	// the Prometheus naming scope rewrites the dash.
	g.prom = sdktally.NewMetricsHandler(newPrometheusScope("0.0.0.0:" + env("METRICS_PORT", "9464")))

	var err error
	// The gRPC metrics interceptor (request and long-poll latency) is bound
	// ONCE, to the handler of the client that dials the connection — clients
	// made later with NewClientFromExisting share the connection and its
	// interceptor. Without a handler here, poll latency went nowhere: not to
	// the lab, and not to Prometheus either.
	g.base, err = client.Dial(client.Options{
		HostPort:       env("TEMPORAL_ADDRESS", "temporal:7233"),
		Namespace:      g.namespace,
		MetricsHandler: newTeeHandler(g.prom, g.agg, "rpc"),
	})
	if err != nil {
		log.Fatalf("dial temporal: %v", err)
	}

	g.itersPerMs = calibrate()
	log.Printf("calibrated CPU work: %.0f sha256 rounds per ms on one core", g.itersPerMs)

	// Block until the controller hands us a config; the first one also sizes
	// the process-wide sticky cache, which cannot change afterwards.
	first := g.waitForConfig()
	g.cacheSize = first.Config.CacheSize
	worker.SetStickyWorkflowCacheSize(g.cacheSize)
	g.apply(first)

	go g.reportLoop()
	g.configLoop()
}

func (g *goWorker) waitForConfig() *LaneConfigVersioned {
	for {
		if c, err := g.fetchConfig(); err == nil {
			return c
		} else {
			log.Printf("waiting for controller: %v", err)
		}
		time.Sleep(2 * time.Second)
	}
}

func (g *goWorker) fetchConfig() (*LaneConfigVersioned, error) {
	resp, err := httpClient.Get(g.controller + "/api/config?lane=" + g.lane)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("controller returned %s", resp.Status)
	}
	var c LaneConfigVersioned
	return &c, json.NewDecoder(resp.Body).Decode(&c)
}

func (g *goWorker) configLoop() {
	for range time.Tick(time.Second) {
		c, err := g.fetchConfig()
		if err != nil || c.Version == g.applied.Load() {
			continue
		}
		g.apply(c)
	}
}

func needsSwap(a, b LaneConfig) bool {
	return a.WFPollers != b.WFPollers || a.WFSlots != b.WFSlots ||
		a.ActPollers != b.ActPollers || a.ActSlots != b.ActSlots
}

func (g *goWorker) apply(next *LaneConfigVersioned) {
	g.cfg.Store(next)
	c := next.Config

	if c.CacheSize != g.cacheSize {
		// The Go SDK sizes its sticky cache once per process (it is shared by
		// every Worker in it), so the only honest way to resize is a restart.
		log.Printf("sticky cache %d -> %d: restarting the Worker process", g.cacheSize, c.CacheSize)
		os.Exit(exitCacheResize)
	}

	if c.CPUs > 0 && c.CPUs != runtime.GOMAXPROCS(0) {
		runtime.GOMAXPROCS(c.CPUs)
		log.Printf("GOMAXPROCS -> %d", c.CPUs)
	}

	// Compare against the options the running Worker was BUILT with, not the
	// previous config. A failed swap leaves the old Worker running, and
	// comparing configs would then see "no change" on the next poll and mark
	// the version applied without ever swapping — the bug this replaced.
	if g.running == nil || needsSwap(*g.running, c) {
		if err := g.swap(c); err != nil {
			log.Printf("config v%d NOT applied, will retry: %v", next.Version, err)
			return
		}
	}
	g.applied.Store(next.Version)
	log.Printf("config v%d applied: %+v", next.Version, c)
}

// swap starts a Worker with the new options BEFORE stopping the old one, so
// the Task Queue never has zero pollers. For a few seconds both run, and the
// slot gauges sum across them — that overlap is real and visible.
func (g *goWorker) swap(c LaneConfig) error {
	g.mu.Lock()
	defer g.mu.Unlock()

	g.swapCount++
	instance := fmt.Sprintf("%s#%d", g.id, g.swapCount)
	// Namespace MUST be repeated: NewClientFromExisting does not inherit it,
	// and options without one silently mean "default". Found when the lab
	// moved to its own Namespace — the Worker kept polling lab-go in
	// "default", where nothing was ever scheduled.
	cl, err := client.NewClientFromExisting(g.base, client.Options{
		Namespace:      g.namespace,
		MetricsHandler: newTeeHandler(g.prom, g.agg, instance),
	})
	if err != nil {
		return fmt.Errorf("swap: client: %w", err)
	}
	w := worker.New(cl, taskQueueFor(g.lane), worker.Options{
		MaxConcurrentWorkflowTaskPollers:       max(c.WFPollers, 2), // SDK minimum is 2
		MaxConcurrentWorkflowTaskExecutionSize: max(c.WFSlots, 2),
		MaxConcurrentActivityTaskPollers:       max(c.ActPollers, 1),
		MaxConcurrentActivityExecutionSize:     max(c.ActSlots, 1),
		WorkerStopTimeout:                      30 * time.Second,
		Identity:                               instance,
	})
	w.RegisterWorkflowWithOptions(LabWorkflow, workflow.RegisterOptions{Name: workflowName})
	w.RegisterActivityWithOptions(g.labActivity, activity.RegisterOptions{Name: activityName})
	if err := w.Start(); err != nil {
		cl.Close()
		return fmt.Errorf("swap: start: %w", err)
	}

	old, oldCl, oldID := g.current, g.currentCl, g.currentID
	g.current, g.currentCl, g.currentID = w, cl, instance
	g.running = &c
	if old != nil {
		go func() {
			old.Stop() // drains in-flight Activities, up to WorkerStopTimeout
			// Each swap gets its own client; close it once its Worker is gone
			// or every swap leaks one. The shared connection stays open.
			oldCl.Close()
			g.agg.dropInstance(oldID)
			log.Printf("drained %s", oldID)
		}()
	}
	return nil
}

// -----------------------------------------------------------------------------
// Activity — reads the LIVE config, so profile changes hit work in flight.
// -----------------------------------------------------------------------------

func (g *goWorker) labActivity(ctx context.Context, in ActivityInput) error {
	c := g.cfg.Load().Config
	d := time.Duration(c.DurationMs) * time.Millisecond
	if c.JitterPct > 0 {
		j := float64(d) * float64(c.JitterPct) / 100
		d += time.Duration((rand.Float64()*2 - 1) * j)
	}
	switch c.Profile {
	case "cpu":
		return burn(ctx, int64(g.itersPerMs*float64(d.Milliseconds())))
	case "io":
		return g.downstream(ctx, d)
	default:
		select {
		case <-time.After(d):
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// burn does a FIXED amount of CPU work, calibrated to take DurationMs on one
// uncontended core. Under contention (more runnable Activities than
// GOMAXPROCS) the same work takes longer in wall time — which is the point.
func burn(ctx context.Context, rounds int64) error {
	buf := []byte("temporal-tuning-lab")
	for i := int64(0); i < rounds; i++ {
		s := sha256.Sum256(buf)
		buf = s[:]
		if i%4096 == 0 && ctx.Err() != nil {
			return ctx.Err()
		}
	}
	return nil
}

func calibrate() float64 {
	const rounds = 200_000
	start := time.Now()
	_ = burn(context.Background(), rounds)
	return rounds / float64(time.Since(start).Milliseconds()+1)
}

// downstream makes a real HTTP call to the controller's fake dependency, which
// admits only IOConcurrency calls at once. Time spent queued there is time the
// Activity holds a slot while the Worker's CPU sits idle.
func (g *goWorker) downstream(ctx context.Context, d time.Duration) error {
	url := fmt.Sprintf("%s/downstream?lane=%s&ms=%d", g.controller, g.lane, d.Milliseconds())
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	// No client timeout here: the Activity's own context bounds it, and the
	// time spent queued at the dependency is the thing being measured.
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("downstream returned %s", resp.Status)
	}
	return nil
}

// -----------------------------------------------------------------------------
// Reporting
// -----------------------------------------------------------------------------

// effectiveCPULimit is the CPU the Worker can actually use: GOMAXPROCS or the
// container's cgroup quota, whichever is smaller. Reporting GOMAXPROCS alone
// broke the CPU-bound diagnosis above the quota: at GOMAXPROCS=8 in a 4-CPU
// container the threshold was 6.8 cores, which the container can never reach,
// so a saturated Worker read as idle and fell through to "poller-bound".
func effectiveCPULimit() float64 {
	limit := float64(runtime.GOMAXPROCS(0))
	if b, err := os.ReadFile("/sys/fs/cgroup/cpu.max"); err == nil {
		var quota, period float64
		if n, _ := fmt.Sscanf(string(b), "%f %f", &quota, &period); n == 2 && period > 0 {
			limit = min(limit, quota/period)
		}
	}
	return limit
}

func cpuSeconds() float64 {
	var ru syscall.Rusage
	_ = syscall.Getrusage(syscall.RUSAGE_SELF, &ru)
	return float64(ru.Utime.Sec+ru.Stime.Sec) + float64(ru.Utime.Usec+ru.Stime.Usec)/1e6
}

func (g *goWorker) reportLoop() {
	lastCPU, lastAt := cpuSeconds(), time.Now()
	for range time.Tick(500 * time.Millisecond) {
		now, cpu := time.Now(), cpuSeconds()
		cores := (cpu - lastCPU) / now.Sub(lastAt).Seconds()
		lastCPU, lastAt = cpu, now

		var ms runtime.MemStats
		runtime.ReadMemStats(&ms)
		counters, gauges, lat := g.agg.snapshot()
		rep := WorkerReport{
			Lane: g.lane, WorkerID: g.id, SDK: "go " + temporal.SDKVersion,
			ConfigVersion: g.applied.Load(), StartedAt: g.startedAt,
			Counters: counters, Gauges: gauges, Latency: lat,
			CPUCores: cores, CPULimit: effectiveCPULimit(),
			RSSMB: float64(ms.Sys) / (1 << 20),
		}
		body, _ := json.Marshal(rep)
		resp, err := httpClient.Post(g.controller+"/api/report", "application/json", bytes.NewReader(body))
		if err == nil {
			resp.Body.Close()
		}
	}
}
