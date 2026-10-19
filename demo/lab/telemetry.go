package main

import (
	"sort"
	"strings"
	"sync"
	"time"

	"go.temporal.io/sdk/client"
)

// WorkerReport is what every Worker — Go or Python — sends the controller
// every 500ms. Both SDKs fill the same shape from their own real metrics, so
// the UI never has to know which language it is looking at.
type WorkerReport struct {
	Lane          string             `json:"lane"`
	WorkerID      string             `json:"workerId"`
	SDK           string             `json:"sdk"`
	ConfigVersion int64              `json:"configVersion"`
	StartedAt     time.Time          `json:"startedAt"`
	Counters      map[string]float64 `json:"counters"` // cumulative since process start
	Gauges        map[string]float64 `json:"gauges"`
	Latency       map[string]Pct     `json:"latency"`  // over the last latencyWindow
	CPUCores      float64            `json:"cpuCores"` // cores in use, measured
	CPULimit      float64            `json:"cpuLimit"` // cores available to the work
	RSSMB         float64            `json:"rssMB"`
	Note          string             `json:"note,omitempty"`
}

type Pct struct {
	P50  float64 `json:"p50"` // seconds
	P95  float64 `json:"p95"`
	Mean float64 `json:"mean"`
	N    int     `json:"n"`
}

const latencyWindow = 10 * time.Second

// Canonical keys. Each SDK maps its own metric names onto these.
//
//	counters: wf_completed act_completed act_failed wft_completed cache_hit cache_miss
//	gauges:   wft_slots_used wft_slots_available act_slots_used act_slots_available
//	          pollers_workflow pollers_sticky pollers_activity cache_size
//	latency:  wft_s2s wft_exec act_s2s act_exec replay wf_e2e poll_wft poll_act
var timerKeys = map[string]string{
	"workflow_task_schedule_to_start_latency": "wft_s2s",
	"workflow_task_execution_latency":         "wft_exec",
	"activity_schedule_to_start_latency":      "act_s2s",
	"activity_execution_latency":              "act_exec",
	"workflow_task_replay_latency":            "replay",
	"workflow_endtoend_latency":               "wf_e2e",
}

// Timers whose COUNT is also a completion counter.
var timerCounts = map[string]string{
	"wft_exec": "wft_completed",
	"act_exec": "act_completed",
}

var counterKeys = map[string]string{
	"workflow_completed":        "wf_completed",
	"activity_execution_failed": "act_failed",
	"sticky_cache_hit":          "cache_hit",
	"sticky_cache_miss":         "cache_miss",
}

// gaugeKey maps a gauge and its tags to a canonical key, or "" to ignore it.
func gaugeKey(name string, tags map[string]string) string {
	switch name {
	case "worker_task_slots_used", "worker_task_slots_available":
		kind := map[string]string{"WorkflowWorker": "wft", "ActivityWorker": "act"}[tags["worker_type"]]
		if kind == "" {
			return ""
		}
		return kind + "_slots_" + strings.TrimPrefix(name, "worker_task_slots_")
	case "num_pollers":
		switch tags["poller_type"] {
		case "workflow_task":
			return "pollers_workflow"
		case "workflow_sticky_task":
			return "pollers_sticky"
		case "activity_task":
			return "pollers_activity"
		}
	case "sticky_cache_size":
		return "cache_size"
	}
	return ""
}

func canonical(name string) string { return strings.TrimPrefix(name, "temporal_") }

// -----------------------------------------------------------------------------
// Aggregator
// -----------------------------------------------------------------------------

type sample struct {
	at time.Time
	v  float64
}

type aggregator struct {
	mu       sync.Mutex
	counters map[string]float64
	// gauges are per Worker instance so that, while a swap has two Workers in
	// the process, slots and pollers are summed across both — and a stopped
	// instance's last values can be dropped instead of lingering.
	gauges  map[string]map[string]float64 // instance -> key -> value
	samples map[string][]sample
}

func newAggregator() *aggregator {
	return &aggregator{
		counters: map[string]float64{},
		gauges:   map[string]map[string]float64{},
		samples:  map[string][]sample{},
	}
}

func (a *aggregator) inc(key string, n float64) {
	a.mu.Lock()
	a.counters[key] += n
	a.mu.Unlock()
}

func (a *aggregator) setGauge(instance, key string, v float64) {
	a.mu.Lock()
	if a.gauges[instance] == nil {
		a.gauges[instance] = map[string]float64{}
	}
	a.gauges[instance][key] = v
	a.mu.Unlock()
}

func (a *aggregator) dropInstance(instance string) {
	a.mu.Lock()
	delete(a.gauges, instance)
	a.mu.Unlock()
}

func (a *aggregator) record(key string, d time.Duration) {
	now := time.Now()
	a.mu.Lock()
	s := append(a.samples[key], sample{now, d.Seconds()})
	if len(s) > 20000 {
		s = s[len(s)-20000:]
	}
	a.samples[key] = s
	if c, ok := timerCounts[key]; ok {
		a.counters[c]++
	}
	a.mu.Unlock()
}

func percentile(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	i := int(p * float64(len(sorted)-1))
	return sorted[i]
}

// snapshot returns cumulative counters, instance-summed gauges and windowed
// percentiles, trimming samples older than the window as it goes.
func (a *aggregator) snapshot() (map[string]float64, map[string]float64, map[string]Pct) {
	cutoff := time.Now().Add(-latencyWindow)
	a.mu.Lock()
	defer a.mu.Unlock()
	counters := make(map[string]float64, len(a.counters))
	for k, v := range a.counters {
		counters[k] = v
	}
	gauges := map[string]float64{}
	for _, g := range a.gauges {
		for k, v := range g {
			gauges[k] += v
		}
	}
	lat := map[string]Pct{}
	for k, s := range a.samples {
		i := sort.Search(len(s), func(i int) bool { return s[i].at.After(cutoff) })
		s = s[i:]
		a.samples[k] = s
		vals := make([]float64, len(s))
		sum := 0.0
		for j, x := range s {
			vals[j] = x.v
			sum += x.v
		}
		sort.Float64s(vals)
		p := Pct{P50: percentile(vals, 0.50), P95: percentile(vals, 0.95), N: len(vals)}
		if len(vals) > 0 {
			p.Mean = sum / float64(len(vals))
		}
		lat[k] = p
	}
	return counters, gauges, lat
}

// -----------------------------------------------------------------------------
// Tee handler: everything still goes to Prometheus (next); the canonical
// subset is also aggregated in-process for the live view.
// -----------------------------------------------------------------------------

type teeHandler struct {
	next     client.MetricsHandler
	agg      *aggregator
	instance string
	tags     map[string]string
}

func newTeeHandler(next client.MetricsHandler, agg *aggregator, instance string) client.MetricsHandler {
	return &teeHandler{next: next, agg: agg, instance: instance, tags: map[string]string{}}
}

func (h *teeHandler) WithTags(t map[string]string) client.MetricsHandler {
	merged := make(map[string]string, len(h.tags)+len(t))
	for k, v := range h.tags {
		merged[k] = v
	}
	for k, v := range t {
		merged[k] = v
	}
	return &teeHandler{next: h.next.WithTags(t), agg: h.agg, instance: h.instance, tags: merged}
}

func (h *teeHandler) Counter(name string) client.MetricsCounter {
	return teeCounter{h.next.Counter(name), h.agg, counterKeys[canonical(name)]}
}

func (h *teeHandler) Gauge(name string) client.MetricsGauge {
	return teeGauge{h.next.Gauge(name), h.agg, h.instance, gaugeKey(canonical(name), h.tags)}
}

func (h *teeHandler) Timer(name string) client.MetricsTimer {
	key := timerKeys[canonical(name)]
	if canonical(name) == "long_request_latency" {
		key = pollKey(h.tags["operation"])
	}
	return teeTimer{h.next.Timer(name), h.agg, key}
}

// pollKey maps a long-poll RPC to a canonical key. Poll latency is what tells
// a poller-bound Worker from a server-bound one: with a backlog waiting, a
// poll that returns fast means the server had the task ready and the Worker
// simply is not asking often enough; a poll that returns slowly means the
// server is slow to hand out work it already has.
func pollKey(operation string) string {
	switch {
	case strings.Contains(operation, "PollActivityTaskQueue"):
		return "poll_act"
	case strings.Contains(operation, "PollWorkflowTaskQueue"):
		return "poll_wft"
	}
	return ""
}

type teeCounter struct {
	next client.MetricsCounter
	agg  *aggregator
	key  string
}

func (c teeCounter) Inc(n int64) {
	c.next.Inc(n)
	if c.key != "" {
		c.agg.inc(c.key, float64(n))
	}
}

type teeGauge struct {
	next     client.MetricsGauge
	agg      *aggregator
	instance string
	key      string
}

func (g teeGauge) Update(v float64) {
	g.next.Update(v)
	if g.key != "" {
		g.agg.setGauge(g.instance, g.key, v)
	}
}

type teeTimer struct {
	next client.MetricsTimer
	agg  *aggregator
	key  string
}

func (t teeTimer) Record(d time.Duration) {
	t.next.Record(d)
	if t.key != "" {
		t.agg.record(t.key, d)
	}
}
