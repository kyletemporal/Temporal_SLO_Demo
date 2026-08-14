// Command monitor polls Temporal Visibility and exports workflow-duration SLIs
// that Prometheus cannot produce on its own.
//
// The gap it fills: every Temporal metric describing workflow outcome is a
// counter over executions that ENDED. An execution that never ends increments
// nothing, so a workflow stuck forever is invisible to metrics-based alerting
// while its business outcome silently never happens. Only duration answers that
// question, and duration for an OPEN execution exists solely in Visibility.
//
// Reproduce the gap this closes with `make chaos-stuck` in demo/.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.temporal.io/sdk/client"

	"github.com/temporal-slo-demo/monitor/internal/config"
	"github.com/temporal-slo-demo/monitor/internal/counter"
	"github.com/temporal-slo-demo/monitor/internal/metrics"
	"github.com/temporal-slo-demo/monitor/internal/monitor"
)

func main() {
	var (
		cfgPath = flag.String("config", "slo-config.yaml", "path to SLO config")
		listen  = flag.String("listen", ":9111", "address for /metrics and /healthz")
		pace    = flag.Duration("pace", 100*time.Millisecond, "delay between Visibility queries")
		address = flag.String("address", "", "Temporal address (overrides config)")
	)
	flag.Parse()

	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		log.Error("loading config", "path", *cfgPath, "error", err)
		os.Exit(1)
	}
	if *address != "" {
		cfg.Deployment.Address = *address
	}
	if len(cfg.WorkflowTypes) == 0 {
		log.Error("config lists no workflow_types; nothing to monitor", "path", *cfgPath)
		os.Exit(1)
	}

	reg := prometheus.NewRegistry()
	reg.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	m := metrics.New(reg)

	c, err := client.Dial(client.Options{
		HostPort:  cfg.Deployment.Address,
		Namespace: cfg.Deployment.Namespace,
		Logger:    log,
	})
	if err != nil {
		log.Error("connecting to Temporal", "address", cfg.Deployment.Address, "error", err)
		os.Exit(1)
	}
	defer c.Close()

	mo := monitor.New(cfg, counter.New(c, cfg.Deployment.Namespace, *pace), m, log)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{Registry: reg}))
	// Liveness only. It deliberately does NOT report the health of Visibility
	// polling: a monitor that removes itself from service when its dependency is
	// struggling takes away the signals you need to diagnose that struggle. Poll
	// health is exported as metrics — alert on last_successful_poll_timestamp.
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprintln(w, "ok")
	})

	srv := &http.Server{Addr: *listen, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		log.Info("serving metrics", "listen", *listen, "namespace", cfg.Deployment.Namespace,
			"workflow_types", len(cfg.WorkflowTypes))
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("metrics server", "error", err)
			stop()
		}
	}()

	runErr := mo.Run(ctx)

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)

	if runErr != nil && !errors.Is(runErr, context.Canceled) {
		log.Error("monitor stopped", "error", runErr)
		os.Exit(1)
	}
	log.Info("shut down cleanly")
}
