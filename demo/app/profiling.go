package main

import (
	"errors"
	"log"
	"net/http"
	"net/http/pprof"
	"os"
	"runtime"
	"time"

	"github.com/grafana/pyroscope-go"
)

// Profiling answers the question traces and metrics both dodge: WHERE IN THE
// CODE did the time or memory go?
//
// The three signals divide cleanly on a Temporal Worker:
//
//	metric  "workflow task execution latency p99 is 400ms"
//	trace   "this execution spent 380ms inside ChargePayment"
//	profile "ChargePayment spends 90% of that in JSON marshalling"
//
// This matters more on a Worker than on a typical service. Workflow code runs on
// a cooperatively-scheduled goroutine and MUST NOT block; a blocking call or a
// CPU-heavy loop in Workflow code shows up as workflow task latency — the
// TMPRL1101 "deadlock detected" case — with no indication of which line is at
// fault. A profile names the line.

// startPprofServer runs net/http/pprof on its OWN listener.
//
// Not on the metrics port: the tally Prometheus reporter owns that server and
// exposes no mux to mount onto. Separating them is the better arrangement
// regardless — the metrics port is scraped by anything on the network, and
// pprof must not be.
//
// THIS ENDPOINT IS SENSITIVE. It exposes goroutine stacks and heap contents,
// which leak internal structure and sometimes payload data, and
// /debug/pprof/profile blocks for the full profile duration. Keep it on a port
// that is not published, and reach it with `docker compose exec` or a port
// forward. It is deliberately NOT published in this stack's compose file.
func startPprofServer(addr string) {
	mux := http.NewServeMux()
	registerPprof(mux)
	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		log.Printf("INFO  pprof listening on %s (not published; use docker compose exec)", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("WARN  pprof server: %v", err)
		}
	}()
}

func registerPprof(mux *http.ServeMux) {
	mux.HandleFunc("/debug/pprof/", pprof.Index)
	mux.HandleFunc("/debug/pprof/cmdline", pprof.Cmdline)
	mux.HandleFunc("/debug/pprof/profile", pprof.Profile)
	mux.HandleFunc("/debug/pprof/symbol", pprof.Symbol)
	mux.HandleFunc("/debug/pprof/trace", pprof.Trace)

	// Block and mutex profiles are OFF unless asked for. They are not free —
	// each adds instrumentation to every blocking operation and every mutex
	// contention event, which is exactly the hot path on a Worker under load.
	// Enable deliberately while investigating, not by default.
	if os.Getenv("PROFILE_CONTENTION") == "1" {
		runtime.SetBlockProfileRate(10000)   // ~1 sample per 10µs blocked
		runtime.SetMutexProfileFraction(100) // ~1 in 100 contention events
		log.Printf("INFO  block and mutex profiling ENABLED (adds overhead)")
	}
}

// initProfiling starts continuous profiling to Pyroscope.
//
// pprof answers "what is it doing right now"; continuous profiling answers "what
// was it doing during the incident, twenty minutes ago". On a Worker that is
// usually the question, because the interesting states — slot exhaustion, a
// replay storm, a stuck Workflow — are transient and nobody is holding a pprof
// session open when they happen.
//
// No-ops when PYROSCOPE_ADDRESS is unset, so profiling is never a startup
// dependency.
func initProfiling(appName string) func() {
	addr := os.Getenv("PYROSCOPE_ADDRESS")
	if addr == "" {
		log.Printf("INFO  continuous profiling disabled (PYROSCOPE_ADDRESS unset)")
		return func() {}
	}

	p, err := pyroscope.Start(pyroscope.Config{
		ApplicationName: appName,
		ServerAddress:   addr,
		Logger:          nil, // pyroscope's own logging is noisy at INFO

		// Tags are labels, and the same cardinality discipline applies as
		// everywhere else in this repo: task_queue is bounded, workflow_id
		// would not be.
		Tags: map[string]string{
			"task_queue": env("TASK_QUEUE", "orders"),
			"namespace":  env("TEMPORAL_NAMESPACE", "default"),
		},

		ProfileTypes: []pyroscope.ProfileType{
			pyroscope.ProfileCPU,
			pyroscope.ProfileAllocObjects,
			pyroscope.ProfileAllocSpace,
			pyroscope.ProfileInuseObjects,
			pyroscope.ProfileInuseSpace,
			pyroscope.ProfileGoroutines,
			// Block and mutex profiles are omitted deliberately: they require
			// the runtime rates above, which cost real overhead on a hot path.
		},
	})
	if err != nil {
		// Never fatal. A profiling backend being down must not stop a Worker
		// from processing Workflows.
		log.Printf("WARN  continuous profiling unavailable: %v", err)
		return func() {}
	}

	log.Printf("INFO  continuous profiling enabled app=%s server=%s", appName, addr)
	return func() { _ = p.Stop() }
}
