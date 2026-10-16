package main

import (
	"context"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"log"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	taskqueuepb "go.temporal.io/api/taskqueue/v1"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
	"google.golang.org/protobuf/types/known/durationpb"
)

//go:embed ui
var uiFS embed.FS

var lanes = []string{"go", "python"}

// -----------------------------------------------------------------------------
// Presets. Named after the bottlenecks in cvilenio/temporal-worker-tuning-simulator
// so the two can be used together: predict it there, then watch the real SDK
// and server do it here. No code is shared with that project.
// -----------------------------------------------------------------------------

type Preset struct {
	Name     string     `json:"name"`
	Title    string     `json:"title"`
	Expect   string     `json:"expect"`
	Config   LaneConfig `json:"config"`
	GoOnly   bool       `json:"goOnly,omitempty"`
	Disabled bool       `json:"-"`
}

var baseline = LaneConfig{
	Enabled: true, Rate: 4, Phases: 3, ActivitiesPerPhase: 2,
	WFPollers: 2, WFSlots: 10, ActPollers: 2, ActSlots: 10,
	CacheSize: 1000, CPUs: 2,
	Profile: "sleep", DurationMs: 150, JitterPct: 20, IOConcurrency: 8,
}

func preset(name, title, expect string, mut func(*LaneConfig)) Preset {
	c := baseline
	mut(&c)
	return Preset{Name: name, Title: title, Expect: expect, Config: c}
}

var presets = []Preset{
	preset("baseline", "Baseline",
		"Queues stay near zero; tasks are matched to waiting pollers as they arrive.",
		func(c *LaneConfig) {}),
	preset("slot-constrained", "Slot constrained",
		"One Activity slot. Every slot busy, CPU idle, Activity backlog and schedule-to-start climb.",
		func(c *LaneConfig) { c.ActSlots = 1; c.DurationMs = 200 }),
	preset("poller-constrained", "Poller constrained",
		"One Activity poller feeding 50 slots at high rate. Watch whether the poller is really the limit on a low-latency network — often it is not, and that is the finding.",
		func(c *LaneConfig) {
			c.ActPollers = 1
			c.ActSlots = 50
			c.Rate = 25
			c.Phases = 2
			c.ActivitiesPerPhase = 4
			c.DurationMs = 50
		}),
	preset("cpu-constrained", "CPU constrained",
		"CPU-bound Activities on one core. Slots fill because each Activity waits for CPU; CPU pins. More slots makes it worse.",
		func(c *LaneConfig) {
			c.Profile = "cpu"
			c.DurationMs = 100
			c.CPUs = 1
			c.ActSlots = 20
			c.Rate = 4
			c.Phases = 2
		}),
	preset("io-constrained", "I/O constrained",
		"Activities call a dependency that admits 2 requests at a time. Slots fill, CPU stays idle, requests queue at the dependency.",
		func(c *LaneConfig) {
			c.Profile = "io"
			c.DurationMs = 200
			c.IOConcurrency = 2
			c.ActSlots = 20
			c.Rate = 3
			c.Phases = 2
		}),
	preset("cache-constrained", "Cache constrained",
		"A sticky cache of 2 for dozens of Workflows in flight. Nearly every Workflow Task misses and replays history. "+
			"On short histories replay is cheap (measured: p95 under 1ms) — the cost grows with history length, which is why thrash hides until histories are long.",
		func(c *LaneConfig) {
			c.CacheSize = 2
			c.Phases = 12
			c.ActivitiesPerPhase = 1
			c.DurationMs = 400
			c.Rate = 1.5
		}),
	preset("add-workers", "Not enough Workers",
		"CPU-bound work with slots already matched to cores, and demand above what 2 cores can do. "+
			"CPU pins and slots are full: the one case here where adding Workers (more cores) is the right answer.",
		func(c *LaneConfig) {
			c.Profile = "cpu"
			c.CPUs = 2
			c.ActSlots = 4
			c.DurationMs = 100
			c.Rate = 8
			c.Phases = 2
		}),
}

// -----------------------------------------------------------------------------
// State
// -----------------------------------------------------------------------------

// QueueStats is the Task Queue as the SERVER sees it.
//
// Backlog, age, dispatch rate and sync-match come from the server's own
// metrics in Prometheus, summed across partitions. DescribeTaskQueue's
// ReportStats looked like the obvious source and is not usable here: on this
// 1.27 server it described only the root partition and read 0 while the
// server's approximate_backlog_count showed 560 tasks across partitions 0-2.
// Pollers still come from DescribeTaskQueue, which reports them correctly.
type QueueStats struct {
	Backlog      int64   `json:"backlog"`
	AgeSeconds   float64 `json:"ageSeconds"`
	DispatchRate float64 `json:"dispatchRate"` // poll_success /s
	SyncMatch    float64 `json:"syncMatch"`    // poll_success_sync / poll_success, -1 if idle
	Pollers      int     `json:"pollers"`
	Source       string  `json:"source"`
	Err          string  `json:"err,omitempty"`
}

type WorkerView struct {
	WorkerReport
	AgeSeconds float64 `json:"ageSeconds"`
	Stale      bool    `json:"stale"`
	Applied    bool    `json:"applied"`
}

type Derived struct {
	WfPerSec, WftPerSec, ActPerSec, ActFailPerSec float64
	WftSlotsUsed, WftSlotsTotal                   float64
	ActSlotsUsed, ActSlotsTotal                   float64
	PollersWF, PollersSticky, PollersAct          float64
	CacheHitRatio, CacheSize                      float64
	CPUCores, CPULimit                            float64
	WftS2sP95, ActS2sP95, WftExecP95, ActExecP95  float64
	ReplayP95, E2EP95                             float64

	// Queue depth by Little's law: L = dispatch rate x mean wait. Both inputs
	// are SDK measurements with no scrape lag. The server's
	// approximate_backlog_count is shown beside it but not trusted alone: in
	// steady runs the two agreed (147 vs 162, 57 vs 55), but after a mass
	// termination the gauge froze at 12 for minutes while the SDK measured a
	// 39s schedule-to-start at 4.7 dispatches/s — a real queue of ~180.
	WftQueueEst, ActQueueEst float64

	// Long-poll RPC latency, p50 over the window. See pollKey in telemetry.go.
	PollWftP50, PollActP50 float64
}

// ServerHealth is the Temporal server's own view of how hard it is working,
// from its metrics in Prometheus. Shared by both lanes: they run on one server.
type ServerHealth struct {
	PersistenceP95 float64 `json:"persistenceP95"` // seconds, all operations
	RespondP95     float64 `json:"respondP95"`     // seconds, Respond{Activity,Workflow}TaskCompleted at the frontend
	Err            string  `json:"err,omitempty"`
}

// Server-bound thresholds, each about 10x the healthy value measured on this
// lab and well below the server-bound value (Postgres capped at 0.3 CPU):
//
//	                         healthy (incl. heavy load)   server-bound
//	persistence p95          2.7 - 4.6 ms                  96 ms
//	task-completion RPC p95  32 - 48 ms                    4.0 - 4.4 s
//	SDK poll p50, queued     2 - 16 ms (poller-bound)      790 - 1000 ms
const (
	persistenceSlow = 0.050
	respondSlow     = 1.0
	pollSlow        = 0.5
)

type LaneSnapshot struct {
	Lane       string       `json:"lane"`
	Version    int64        `json:"version"`
	Config     LaneConfig   `json:"config"`
	Workers    []WorkerView `json:"workers"`
	WF         QueueStats   `json:"wf"`
	Act        QueueStats   `json:"act"`
	Started    int64        `json:"started"`
	StartErrs  int64        `json:"startErrors"`
	Dropped    int64        `json:"startsDropped"`
	Downstream struct {
		Limit, InFlight, Waiting int
	} `json:"downstream"`
	Derived   Derived      `json:"derived"`
	Server    ServerHealth `json:"server"`
	Diagnosis []string     `json:"diagnosis"`
}

type Snapshot struct {
	At    time.Time                `json:"at"`
	Lanes map[string]*LaneSnapshot `json:"lanes"`
}

type counterPoint struct {
	at time.Time
	c  map[string]float64
}

type laneState struct {
	mu      sync.Mutex
	version int64
	cfg     LaneConfig
	reports map[string]WorkerReport
	seenAt  map[string]time.Time
	history map[string][]counterPoint // per worker, for rates
	wf, act QueueStats
	started atomic.Int64
	errs    atomic.Int64 // starts the server rejected
	dropped atomic.Int64 // starts skipped because this lane's client was saturated
	sem     chan struct{}
	ds      *limiter
}

type controller struct {
	tc      client.Client
	ns      string
	prom    string
	lanes   map[string]*laneState
	version atomic.Int64

	healthMu sync.Mutex
	health   ServerHealth

	histMu  sync.Mutex
	history []Snapshot // last 10 minutes, 1s apart

	subsMu sync.Mutex
	subs   map[chan []byte]struct{}
}

// labNamespace is the lab's own Temporal Namespace. NOT "default": the demo
// application's SLOs are scoped to "default", and the lab's presets create
// bottlenecks on purpose — run in "default", one slot-constrained preset
// (37.7s schedule-to-start) drained the demo's worker_task_delivery budget.
const labNamespace = "lab"

func runController() {
	ns := env("TEMPORAL_NAMESPACE", labNamespace)
	tc, err := client.Dial(client.Options{
		HostPort:  env("TEMPORAL_ADDRESS", "temporal:7233"),
		Namespace: ns,
	})
	if err != nil {
		log.Fatalf("dial temporal: %v", err)
	}
	// Before serving anything: Workers wait for the controller's config, so
	// they never poll a Namespace that does not exist yet.
	ensureNamespace(tc, ns)
	c := &controller{
		tc: tc, ns: ns,
		prom:  env("PROMETHEUS_URL", "http://prometheus:9090"),
		lanes: map[string]*laneState{},
		subs:  map[chan []byte]struct{}{},
	}
	for _, l := range lanes {
		cfg := baseline
		// Start idle. Load begins when someone presses Start in the UI —
		// a lab that generates traffic the moment the stack comes up skews
		// every other dashboard on the stack.
		cfg.Enabled = false
		ls := &laneState{
			cfg: cfg, reports: map[string]WorkerReport{}, seenAt: map[string]time.Time{},
			history: map[string][]counterPoint{}, ds: newLimiter(cfg.IOConcurrency),
			sem: make(chan struct{}, maxConcurrentStarts),
		}
		ls.version = c.version.Add(1)
		c.lanes[l] = ls
		go c.loadLoop(l, ls)
		go c.statsLoop(l, ls)
	}
	go c.snapshotLoop()
	go c.healthLoop()

	mux := http.NewServeMux()
	ui, _ := fs.Sub(uiFS, "ui")
	mux.Handle("/", http.FileServer(http.FS(ui)))
	mux.HandleFunc("/api/config", c.handleConfig) // GET for Workers; POST is guarded inside
	mux.HandleFunc("/api/report", mutating(c.handleReport))
	mux.HandleFunc("/api/presets", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, presets) })
	mux.HandleFunc("/api/preset", mutating(c.handlePreset))
	mux.HandleFunc("/api/burst", mutating(c.handleBurst))
	mux.HandleFunc("/api/stream", c.handleStream)
	mux.HandleFunc("/downstream", c.handleDownstream)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })

	addr := ":" + env("HTTP_PORT", "8095")
	log.Printf("tuning lab listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}

// ensureNamespace registers the lab's Namespace if it is missing. Idempotent:
// AlreadyExists is success. Retries while the server is still starting.
func ensureNamespace(tc client.Client, ns string) {
	for attempt := 1; ; attempt++ {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_, err := tc.WorkflowService().RegisterNamespace(ctx, &workflowservice.RegisterNamespaceRequest{
			Namespace:                        ns,
			Description:                      "Worker tuning lab (demo/lab). Isolated from the demo app's SLOs.",
			WorkflowExecutionRetentionPeriod: durationpb.New(24 * time.Hour),
		})
		cancel()
		var exists *serviceerror.NamespaceAlreadyExists
		if err == nil || errors.As(err, &exists) {
			log.Printf("namespace %q ready", ns)
			return
		}
		if attempt >= 30 {
			log.Fatalf("register namespace %q: %v", ns, err)
		}
		log.Printf("register namespace %q (attempt %d): %v", ns, attempt, err)
		time.Sleep(2 * time.Second)
	}
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func (c *controller) lane(w http.ResponseWriter, r *http.Request) (string, *laneState, bool) {
	l := r.URL.Query().Get("lane")
	ls, ok := c.lanes[l]
	if !ok {
		http.Error(w, "unknown lane", http.StatusBadRequest)
	}
	return l, ls, ok
}

// GET: Workers poll this. POST: the UI sends a full LaneConfig.
// mutating guards every endpoint that changes state: POST only, and a JSON
// Content-Type. The JSON requirement is the CSRF defence — a cross-site form
// or <img> can only send "simple" requests, and application/json is not one,
// so a browser must preflight it and the controller (which sends no CORS
// headers) refuses. Without it, any web page open on the same machine could
// start up to 2000 Workflows with one POST to /api/burst.
func mutating(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !jsonPost(w, r) {
			return
		}
		h(w, r)
	}
}

func jsonPost(w http.ResponseWriter, r *http.Request) bool {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return false
	}
	if ct := r.Header.Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		http.Error(w, "Content-Type must be application/json", http.StatusUnsupportedMediaType)
		return false
	}
	return true
}

func (c *controller) handleConfig(w http.ResponseWriter, r *http.Request) {
	_, ls, ok := c.lane(w, r)
	if !ok {
		return
	}
	if r.Method != http.MethodGet {
		if !jsonPost(w, r) {
			return
		}
		var cfg LaneConfig
		if err := json.NewDecoder(r.Body).Decode(&cfg); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		if err := validate(&cfg); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		c.setConfig(ls, cfg)
	}
	ls.mu.Lock()
	defer ls.mu.Unlock()
	writeJSON(w, LaneConfigVersioned{Version: ls.version, Config: ls.cfg})
}

func clamp(v *int, lo, hi int) {
	*v = max(lo, min(hi, *v))
}

func validate(c *LaneConfig) error {
	switch c.Profile {
	case "sleep", "cpu", "io":
	default:
		return fmt.Errorf("profile must be sleep, cpu or io")
	}
	c.Rate = max(0, min(200, c.Rate))
	clamp(&c.Phases, 1, 50)
	clamp(&c.ActivitiesPerPhase, 1, 20)
	clamp(&c.WFPollers, 2, 64)
	clamp(&c.WFSlots, 2, 500)
	clamp(&c.ActPollers, 1, 64)
	clamp(&c.ActSlots, 1, 1000)
	clamp(&c.CacheSize, 1, 10000)
	clamp(&c.CPUs, 1, 16)
	clamp(&c.DurationMs, 1, 60000)
	clamp(&c.JitterPct, 0, 100)
	clamp(&c.IOConcurrency, 1, 1000)
	return nil
}

func (c *controller) setConfig(ls *laneState, cfg LaneConfig) {
	ls.mu.Lock()
	ls.cfg = cfg
	ls.version = c.version.Add(1)
	ls.mu.Unlock()
	ls.ds.setLimit(cfg.IOConcurrency)
}

func (c *controller) handlePreset(w http.ResponseWriter, r *http.Request) {
	_, ls, ok := c.lane(w, r)
	if !ok {
		return
	}
	name := r.URL.Query().Get("name")
	for _, p := range presets {
		if p.Name == name {
			cfg := p.Config
			c.setConfig(ls, cfg)
			writeJSON(w, cfg)
			return
		}
	}
	http.Error(w, "unknown preset", http.StatusBadRequest)
}

func (c *controller) handleReport(w http.ResponseWriter, r *http.Request) {
	var rep WorkerReport
	if err := json.NewDecoder(r.Body).Decode(&rep); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	ls, ok := c.lanes[rep.Lane]
	if !ok {
		http.Error(w, "unknown lane", http.StatusBadRequest)
		return
	}
	now := time.Now()
	ls.mu.Lock()
	ls.reports[rep.WorkerID] = rep
	ls.seenAt[rep.WorkerID] = now
	h := append(ls.history[rep.WorkerID], counterPoint{now, rep.Counters})
	// A restarted Worker reports counters from zero; drop the old run.
	if len(h) > 1 && h[len(h)-1].c["act_completed"] < h[len(h)-2].c["act_completed"] {
		h = h[len(h)-1:]
	}
	for len(h) > 1 && now.Sub(h[0].at) > 6*time.Second {
		h = h[1:]
	}
	ls.history[rep.WorkerID] = h
	ls.mu.Unlock()
	w.WriteHeader(http.StatusNoContent)
}

func (c *controller) handleBurst(w http.ResponseWriter, r *http.Request) {
	l, ls, ok := c.lane(w, r)
	if !ok {
		return
	}
	n, _ := strconv.Atoi(r.URL.Query().Get("n"))
	n = max(1, min(n, 2000))
	ls.mu.Lock()
	cfg := ls.cfg
	ls.mu.Unlock()
	go func() {
		for i := 0; i < n; i++ {
			c.start(l, ls, cfg)
		}
	}()
	writeJSON(w, map[string]int{"started": n})
}

// -----------------------------------------------------------------------------
// Load generator: open loop. Starts are fired at the configured rate whether
// or not earlier Workflows have finished — a closed loop would slow itself
// down to match the Workers and hide every backlog this lab exists to show.
// -----------------------------------------------------------------------------

// Each lane has its own start semaphore (laneState.sem). One global one let a
// backed-up lane's starts be counted as the OTHER lane's errors.
const maxConcurrentStarts = 64

func (c *controller) loadLoop(l string, ls *laneState) {
	var tokens float64
	const tick = 100 * time.Millisecond
	for range time.Tick(tick) {
		ls.mu.Lock()
		cfg := ls.cfg
		ls.mu.Unlock()
		if !cfg.Enabled || cfg.Rate <= 0 {
			tokens = 0
			continue
		}
		tokens += cfg.Rate * tick.Seconds()
		for ; tokens >= 1; tokens-- {
			select {
			case ls.sem <- struct{}{}:
				go func() {
					defer func() { <-ls.sem }()
					c.start(l, ls, cfg)
				}()
			default:
				ls.dropped.Add(1) // the client is saturated: count it, don't queue it
			}
		}
	}
}

var bootID = time.Now().Unix()

func (c *controller) start(l string, ls *laneState, cfg LaneConfig) {
	n := ls.started.Add(1)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err := c.tc.ExecuteWorkflow(ctx, client.StartWorkflowOptions{
		ID:                       fmt.Sprintf("lab-%s-%d-%d", l, bootID, n),
		TaskQueue:                taskQueueFor(l),
		WorkflowExecutionTimeout: 15 * time.Minute,
	}, workflowName, WorkflowInput{Lane: l, Phases: cfg.Phases, ActivitiesPerPhase: cfg.ActivitiesPerPhase})
	if err != nil {
		ls.errs.Add(1)
	}
}

// -----------------------------------------------------------------------------
// Server-side truth: the Task Queue as the Temporal server sees it.
// -----------------------------------------------------------------------------

func (c *controller) statsLoop(l string, ls *laneState) {
	for range time.Tick(time.Second) {
		wf := QueueStats{Pollers: c.pollers(l, enumspb.TASK_QUEUE_TYPE_WORKFLOW)}
		act := QueueStats{Pollers: c.pollers(l, enumspb.TASK_QUEUE_TYPE_ACTIVITY)}
		if err := c.promQueue(l, &wf, &act); err != nil {
			wf.Err, act.Err = err.Error(), err.Error()
		}
		ls.mu.Lock()
		ls.wf, ls.act = wf, act
		ls.mu.Unlock()
	}
}

func (c *controller) pollers(l string, t enumspb.TaskQueueType) int {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	r, err := c.tc.WorkflowService().DescribeTaskQueue(ctx, &workflowservice.DescribeTaskQueueRequest{
		Namespace:     c.ns,
		TaskQueue:     &taskqueuepb.TaskQueue{Name: taskQueueFor(l), Kind: enumspb.TASK_QUEUE_KIND_NORMAL},
		TaskQueueType: t,
	})
	if err != nil {
		return -1
	}
	return len(r.GetPollers())
}

// The server's Prometheus reporter rewrites "-" to "_" in label values, so
// Task Queue "lab-go" is taskqueue="lab_go" in every server metric.
func promTaskQueue(l string) string { return strings.ReplaceAll(taskQueueFor(l), "-", "_") }

func (c *controller) promQueue(l string, wf, act *QueueStats) error {
	tq := promTaskQueue(l)
	sel := fmt.Sprintf(`{namespace=%q,taskqueue=%q}`, c.ns, tq)
	queries := map[string]string{
		"backlog":  `sum by (task_type) (approximate_backlog_count` + sel + `)`,
		"age":      `max by (task_type) (approximate_backlog_age_seconds` + sel + `)`,
		"dispatch": `sum by (task_type) (rate(poll_success` + sel + `[1m]))`,
		"sync":     `sum by (task_type) (rate(poll_success_sync` + sel + `[1m]))`,
	}
	res := map[string]map[string]float64{}
	for k, q := range queries {
		m, err := promQuery(c.prom, q)
		if err != nil {
			return err
		}
		res[k] = m
	}
	for _, x := range []struct {
		q    *QueueStats
		kind string
	}{{wf, "Workflow"}, {act, "Activity"}} {
		x.q.Source = "server metrics via Prometheus (10s scrape)"
		x.q.Backlog = int64(res["backlog"][x.kind])
		x.q.AgeSeconds = res["age"][x.kind]
		x.q.DispatchRate = res["dispatch"][x.kind]
		x.q.SyncMatch = -1
		if x.q.DispatchRate > 0 {
			x.q.SyncMatch = res["sync"][x.kind] / x.q.DispatchRate
		}
	}
	return nil
}

// promClient has a timeout: a hung Prometheus otherwise froze the stats loop
// with no error shown.
var promClient = &http.Client{Timeout: 5 * time.Second}

// promQuery runs an instant query and returns value by task_type.
func promQuery(base, q string) (map[string]float64, error) {
	resp, err := promClient.Get(base + "/api/v1/query?query=" + url.QueryEscape(q))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var body struct {
		Status string `json:"status"`
		Error  string `json:"error"`
		Data   struct {
			Result []struct {
				Metric map[string]string `json:"metric"`
				Value  [2]any            `json:"value"`
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	if body.Status != "success" {
		return nil, fmt.Errorf("prometheus: %s", body.Error)
	}
	out := map[string]float64{}
	for _, r := range body.Data.Result {
		s, _ := r.Value[1].(string)
		v, _ := strconv.ParseFloat(s, 64)
		// histogram_quantile returns NaN with no traffic, and encoding/json
		// refuses NaN — one would break the whole live stream.
		if math.IsNaN(v) || math.IsInf(v, 0) {
			continue
		}
		out[r.Metric["task_type"]] = v
	}
	return out, nil
}

func (c *controller) serverHealth() ServerHealth {
	c.healthMu.Lock()
	defer c.healthMu.Unlock()
	return c.health
}

func (c *controller) healthLoop() {
	queries := map[string]string{
		"persistence": `histogram_quantile(0.95, sum by (le) (rate(persistence_latency_bucket[1m])))`,
		"respond": `histogram_quantile(0.95, sum by (le) (rate(service_latency_bucket{` +
			`operation=~"RespondActivityTaskCompleted|RespondWorkflowTaskCompleted"}[1m])))`,
	}
	for range time.Tick(2 * time.Second) {
		var h ServerHealth
		for k, q := range queries {
			m, err := promQuery(c.prom, q)
			if err != nil {
				h.Err = err.Error()
				break
			}
			switch k {
			case "persistence":
				h.PersistenceP95 = m[""]
			case "respond":
				h.RespondP95 = m[""]
			}
		}
		c.healthMu.Lock()
		c.health = h
		c.healthMu.Unlock()
	}
}

// -----------------------------------------------------------------------------
// The fake dependency for the "io" profile: admits IOConcurrency requests at a
// time per lane; the rest wait. Real HTTP, real waiting, no CPU.
// -----------------------------------------------------------------------------

type limiter struct {
	mu                       sync.Mutex
	cond                     *sync.Cond
	limit, inflight, waiting int
}

func newLimiter(n int) *limiter {
	l := &limiter{limit: n}
	l.cond = sync.NewCond(&l.mu)
	return l
}

func (l *limiter) setLimit(n int) {
	l.mu.Lock()
	l.limit = n
	l.mu.Unlock()
	l.cond.Broadcast()
}

func (l *limiter) acquire() {
	l.mu.Lock()
	l.waiting++
	for l.inflight >= l.limit {
		l.cond.Wait()
	}
	l.waiting--
	l.inflight++
	l.mu.Unlock()
}

func (l *limiter) release() {
	l.mu.Lock()
	l.inflight--
	l.mu.Unlock()
	l.cond.Broadcast()
}

func (l *limiter) stats() (int, int, int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.limit, l.inflight, l.waiting
}

func (c *controller) handleDownstream(w http.ResponseWriter, r *http.Request) {
	_, ls, ok := c.lane(w, r)
	if !ok {
		return
	}
	ms, _ := strconv.Atoi(r.URL.Query().Get("ms"))
	ls.ds.acquire()
	defer ls.ds.release()
	select {
	case <-time.After(time.Duration(ms) * time.Millisecond):
	case <-r.Context().Done():
	}
}

// -----------------------------------------------------------------------------
// Snapshots, rates and diagnosis
// -----------------------------------------------------------------------------

func rate(h []counterPoint, key string) float64 {
	if len(h) < 2 {
		return 0
	}
	a, b := h[0], h[len(h)-1]
	dt := b.at.Sub(a.at).Seconds()
	if dt <= 0 {
		return 0
	}
	return (b.c[key] - a.c[key]) / dt
}

func (c *controller) laneSnapshot(l string, ls *laneState) *LaneSnapshot {
	now := time.Now()
	ls.mu.Lock()
	s := &LaneSnapshot{Lane: l, Version: ls.version, Config: ls.cfg, WF: ls.wf, Act: ls.act}
	var d Derived
	var hits, misses float64
	for id, rep := range ls.reports {
		age := now.Sub(ls.seenAt[id])
		if age > 30*time.Second {
			delete(ls.reports, id)
			delete(ls.seenAt, id)
			delete(ls.history, id)
			continue
		}
		v := WorkerView{WorkerReport: rep, AgeSeconds: age.Seconds(),
			Stale: age > 3*time.Second, Applied: rep.ConfigVersion == ls.version}
		s.Workers = append(s.Workers, v)
		if v.Stale {
			continue
		}
		h := ls.history[id]
		d.WfPerSec += rate(h, "wf_completed")
		d.WftPerSec += rate(h, "wft_completed")
		d.ActPerSec += rate(h, "act_completed")
		d.ActFailPerSec += rate(h, "act_failed")
		hits += rate(h, "cache_hit")
		misses += rate(h, "cache_miss")
		g := rep.Gauges
		d.WftSlotsUsed += g["wft_slots_used"]
		d.WftSlotsTotal += g["wft_slots_used"] + g["wft_slots_available"]
		d.ActSlotsUsed += g["act_slots_used"]
		d.ActSlotsTotal += g["act_slots_used"] + g["act_slots_available"]
		d.PollersWF += g["pollers_workflow"]
		d.PollersSticky += g["pollers_sticky"]
		d.PollersAct += g["pollers_activity"]
		d.CacheSize += g["cache_size"]
		d.CPUCores += rep.CPUCores
		d.CPULimit += rep.CPULimit
		lat := rep.Latency
		d.WftS2sP95 = max(d.WftS2sP95, lat["wft_s2s"].P95)
		d.ActS2sP95 = max(d.ActS2sP95, lat["act_s2s"].P95)
		d.WftExecP95 = max(d.WftExecP95, lat["wft_exec"].P95)
		d.ActExecP95 = max(d.ActExecP95, lat["act_exec"].P95)
		d.ReplayP95 = max(d.ReplayP95, lat["replay"].P95)
		d.E2EP95 = max(d.E2EP95, lat["wf_e2e"].P95)
		d.PollWftP50 = max(d.PollWftP50, lat["poll_wft"].P50)
		d.PollActP50 = max(d.PollActP50, lat["poll_act"].P50)
		d.WftQueueEst += rate(h, "wft_completed") * lat["wft_s2s"].Mean
		d.ActQueueEst += rate(h, "act_completed") * lat["act_s2s"].Mean
	}
	if hits+misses > 0 {
		d.CacheHitRatio = hits / (hits + misses)
	} else {
		d.CacheHitRatio = -1
	}
	ls.mu.Unlock()

	s.Started, s.StartErrs, s.Dropped = ls.started.Load(), ls.errs.Load(), ls.dropped.Load()
	s.Downstream.Limit, s.Downstream.InFlight, s.Downstream.Waiting = ls.ds.stats()
	s.Derived = d
	s.Server = c.serverHealth()
	s.Diagnosis = diagnose(s)
	return s
}

// diagnose turns the measured signals into the question an operator should be
// asking. Heuristics, and labelled as such in the UI — but every input is a
// real measurement, not a modelled one.
func diagnose(s *LaneSnapshot) []string {
	d := s.Derived
	var out []string
	if len(s.Workers) == 0 {
		return []string{"No Worker is reporting for this lane."}
	}
	live := 0
	for _, w := range s.Workers {
		if !w.Stale {
			live++
		}
	}
	if live == 0 {
		return []string{"Worker restarting (a sticky cache resize restarts the Go Worker process)."}
	}

	// Queueing evidence: the SDK's schedule-to-start. The server's backlog
	// gauge is shown in the UI but not trusted here (see Derived.ActQueueEst).
	queued := func(est, s2s float64) bool { return s2s > 1 || est >= 10 }
	wfQ, actQ := queued(d.WftQueueEst, d.WftS2sP95), queued(d.ActQueueEst, d.ActS2sP95)

	// CPU first. A saturated Worker starves its OWN pollers and Workflow Tasks
	// as well as Activities, so slots can look free and the queue can look
	// poller-bound when the cause is CPU — measured on this lab: CPU 1.00/1,
	// Activity slots 6 of 20 busy, Workflow Task schedule-to-start 20s.
	python := strings.HasPrefix(s.Workers[0].SDK, "python")
	if (wfQ || actQ) && d.CPULimit > 0 && d.CPUCores >= 0.85*d.CPULimit && python {
		out = append(out, fmt.Sprintf("GIL-bound: the Worker is using %.2f cores — Python runs bytecode on one core at a time — and tasks are queueing "+
			"(Workflow Task schedule-to-start p95 %.1fs, Activity %.1fs). CPU-bound Activities in threads also starve the event loop that runs Workflow code, "+
			"so Workflow Tasks back up too. More slots add threads, not cores: run CPU-bound Activities in a ProcessPoolExecutor or a separate Worker process.",
			d.CPUCores, d.WftS2sP95, d.ActS2sP95))
	} else if (wfQ || actQ) && d.CPULimit > 0 && d.CPUCores >= 0.85*d.CPULimit {
		out = append(out, fmt.Sprintf("CPU-bound: the Worker is using %.2f of %.0f cores and tasks are queueing (Activity schedule-to-start p95 %.1fs, Workflow Task %.1fs). "+
			"A starved Worker also polls slowly, so free slots here do not mean pollers are the problem. Adding slots makes it worse; add CPU or Workers.",
			d.CPUCores, d.CPULimit, d.ActS2sP95, d.WftS2sP95))
	} else {
		check := func(kind string, q QueueStats, est, s2s, used, total, pollers, pollP50 float64, profileIO bool) {
			full := total > 0 && used >= 0.9*total
			wait := fmt.Sprintf("~%.0f queued, schedule-to-start p95 %.1fs", est, s2s)
			// Server-bound is checked BEFORE the slot cases: a slow server holds
			// slots open until its completion RPCs return, which would
			// otherwise read as slot-bound.
			srv := s.Server
			if srv.Err == "" && (srv.PersistenceP95 >= persistenceSlow || srv.RespondP95 >= respondSlow || (!full && pollP50 >= pollSlow)) {
				out = append(out, fmt.Sprintf("%s %s: server-bound. The Temporal server is slow to hand out and record work — persistence p95 %.0fms, "+
					"task-completion RPC p95 %.2fs, polls taking %.2fs with tasks waiting. Worker CPU is idle and slots are free; "+
					"more pollers, slots or Workers will not help. Look at the server and its database.",
					kind, wait, 1000*srv.PersistenceP95, srv.RespondP95, pollP50))
				return
			}
			// Full slots are checked BEFORE "nobody polling": a Worker with every
			// slot busy stops polling by design, so zero active pollers is the
			// normal sign of a slot-bound Worker, not of a missing one.
			switch {
			case full && profileIO && s.Downstream.Waiting > 0:
				out = append(out, fmt.Sprintf("%s %s: every slot is busy waiting on the dependency (%d calls queued there), CPU idle — downstream-bound. More slots or Workers only add pressure on it.",
					kind, wait, s.Downstream.Waiting))
			case full:
				out = append(out, fmt.Sprintf("%s %s: every slot busy, CPU idle — slot-bound. Raise the slot count before adding Workers.", kind, wait))
			case q.Pollers == 0 && pollers == 0:
				out = append(out, fmt.Sprintf("%s %s and nothing is polling the queue.", kind, wait))
			default:
				out = append(out, fmt.Sprintf("%s %s while %.0f of %.0f slots are free and CPU is idle, and each poll returns in %.0fms — the server has the tasks ready "+
					"and the Worker is not asking often enough: poller-bound. Raise the poller count.",
					kind, wait, total-used, total, 1000*pollP50))
			}
		}
		if wfQ {
			check("Workflow Tasks:", s.WF, d.WftQueueEst, d.WftS2sP95, d.WftSlotsUsed, d.WftSlotsTotal, d.PollersWF+d.PollersSticky, d.PollWftP50, false)
		}
		if actQ {
			check("Activities:", s.Act, d.ActQueueEst, d.ActS2sP95, d.ActSlotsUsed, d.ActSlotsTotal, d.PollersAct, d.PollActP50, s.Config.Profile == "io")
		}
	}

	if d.CacheHitRatio >= 0 && d.CacheHitRatio < 0.8 {
		out = append(out, fmt.Sprintf("Sticky cache hit ratio %.0f%%: most Workflow Tasks replay history from the start (replay p95 %.1fms). Raise the cache size, and the memory to hold it.",
			100*d.CacheHitRatio, 1000*d.ReplayP95))
	}
	if d.ActFailPerSec > 0.1 {
		out = append(out, fmt.Sprintf("Activities failing at %.1f/s.", d.ActFailPerSec))
	}
	if len(out) == 0 {
		if s.Config.Enabled && s.Config.Rate > 0 {
			out = append(out, "Keeping up: no backlog on either queue and schedule-to-start under a second.")
		} else {
			out = append(out, "Idle. Press Start load to generate traffic.")
		}
	}
	return out
}

func (c *controller) snapshotLoop() {
	for range time.Tick(time.Second) {
		snap := Snapshot{At: time.Now(), Lanes: map[string]*LaneSnapshot{}}
		for _, l := range lanes {
			snap.Lanes[l] = c.laneSnapshot(l, c.lanes[l])
		}
		c.histMu.Lock()
		c.history = append(c.history, snap)
		if len(c.history) > 600 {
			c.history = c.history[len(c.history)-600:]
		}
		c.histMu.Unlock()

		b, _ := json.Marshal(snap)
		c.subsMu.Lock()
		for ch := range c.subs {
			select {
			case ch <- b:
			default: // slow client: drop a frame rather than block everyone
			}
		}
		c.subsMu.Unlock()
	}
}

// Server-Sent Events: a full history once, then one snapshot per second.
func (c *controller) handleStream(w http.ResponseWriter, r *http.Request) {
	fl, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")

	c.histMu.Lock()
	hist, _ := json.Marshal(c.history)
	c.histMu.Unlock()
	fmt.Fprintf(w, "event: history\ndata: %s\n\n", hist)
	fl.Flush()

	ch := make(chan []byte, 4)
	c.subsMu.Lock()
	c.subs[ch] = struct{}{}
	c.subsMu.Unlock()
	defer func() {
		c.subsMu.Lock()
		delete(c.subs, ch)
		c.subsMu.Unlock()
	}()
	for {
		select {
		case b := <-ch:
			fmt.Fprintf(w, "data: %s\n\n", b)
			fl.Flush()
		case <-r.Context().Done():
			return
		}
	}
}
