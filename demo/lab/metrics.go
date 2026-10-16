package main

import (
	"log"
	"time"

	"github.com/uber-go/tally/v4"
	tallyprom "github.com/uber-go/tally/v4/prometheus"
	sdktally "go.temporal.io/sdk/contrib/tally"

	// Upstream client_golang, NOT the m3db fork. Older versions of tally v4
	// typed ConfigurationOptions.Registry against the fork, and a lot of
	// copy-pasted examples still import it; against current tally that is a
	// type error, because the field is a *prometheus.Registry from here.
	prom "github.com/prometheus/client_golang/prometheus"
)

// newPrometheusScope stands up the SDK metrics endpoint.
//
// This is the step people skip, and skipping it is why the "Worker Fleet
// Health" row of the dashboard renders empty. SDK metrics do not exist until
// the application emits them — no amount of Prometheus configuration conjures
// them into being.
//
// Two details that matter for the dashboard queries:
//
//  1. TimerType "histogram" produces the *_bucket series that histogram_quantile
//     needs. With the default timer type you get summaries, and every
//     schedule-to-start panel silently returns nothing.
//
//  2. The Go SDK emits durations in SECONDS, so the metric names carry a
//     _seconds suffix (temporal_activity_schedule_to_start_latency_seconds_bucket).
//     TypeScript, Python, and .NET emit MILLISECONDS with no suffix. The
//     dashboard shipped alongside this demo assumes the Go/Java form.
func newPrometheusScope(listenAddress string) tally.Scope {
	cfg := tallyprom.Configuration{
		ListenAddress: listenAddress,
		TimerType:     "histogram",
	}

	reporter, err := cfg.NewReporter(
		tallyprom.ConfigurationOptions{
			Registry: prom.NewRegistry(),
			OnError: func(err error) {
				log.Println("prometheus reporter error:", err)
			},
		},
	)
	if err != nil {
		log.Fatalln("failed to create prometheus reporter:", err)
	}

	scopeOpts := tally.ScopeOptions{
		CachedReporter: reporter,
		Separator:      tallyprom.DefaultSeparator,
		// Rewrites characters Prometheus rejects (dots, dashes) into
		// underscores. Without this, tags like task-queue break the scrape.
		SanitizeOptions: &sdktally.PrometheusSanitizeOptions,
	}

	scope, _ := tally.NewRootScope(scopeOpts, time.Second)

	// Applies Temporal's canonical naming (the temporal_ prefix and the
	// _total / _seconds suffixes the documented queries expect).
	scope = sdktally.NewPrometheusNamingScope(scope)

	log.Printf("SDK metrics listening on %s/metrics", listenAddress)
	return scope
}
