package main

import (
	"context"
	"log"

	"go.temporal.io/sdk/client"
	sdktally "go.temporal.io/sdk/contrib/tally"
	"go.temporal.io/sdk/interceptor"
	"go.temporal.io/sdk/worker"
)

func runWorker() {
	scope := newPrometheusScope("0.0.0.0:" + env("METRICS_PORT", "8077"))

	ctx := context.Background()

	// Tracing and profiling are both additive and both fail open. A collector
	// or profiling backend that is down must never stop a Worker from polling —
	// observability that can take production with it is worse than none.
	shutdownTracing, err := initTracing(ctx, "temporal-worker")
	if err != nil {
		log.Printf("WARN  tracing setup failed, continuing without it: %v", err)
		shutdownTracing = func(context.Context) error { return nil }
	}
	defer func() { _ = shutdownTracing(ctx) }()

	// Agent metrics ride the same OTLP endpoint as traces and fail open the same
	// way. Started here rather than only on the agent Worker because a Worker
	// registers every Workflow in the binary; which ones it actually runs is
	// decided by TASK_QUEUE, not by what is compiled in.
	// OTEL_SERVICE_NAME rather than a literal.
	//
	// Measured: with a hardcoded name, the order Worker and the agent Worker
	// both reported service_name="temporal-worker" on their gen_ai series and
	// were indistinguishable in Prometheus. temporal_task_queue told them apart
	// by luck, not by design. The env var is the standard OTel knob and compose
	// sets it per service.
	shutdownAgentMetrics, err := initAgentMetrics(ctx, env("OTEL_SERVICE_NAME", "temporal-worker"))
	if err != nil {
		log.Printf("WARN  agent metrics setup failed, continuing without them: %v", err)
		shutdownAgentMetrics = func(context.Context) error { return nil }
	}
	defer func() { _ = shutdownAgentMetrics(ctx) }()

	stopProfiling := initProfiling("temporal-worker")
	startPprofServer("0.0.0.0:" + env("PPROF_PORT", "6060"))
	defer stopProfiling()

	var interceptors []interceptor.ClientInterceptor
	if ti, err := tracingInterceptor(); err != nil {
		log.Printf("WARN  tracing interceptor unavailable: %v", err)
	} else {
		interceptors = append(interceptors, ti)
	}

	c, err := dialTemporal(client.Options{
		HostPort:       env("TEMPORAL_ADDRESS", "localhost:7233"),
		Namespace:      env("TEMPORAL_NAMESPACE", "default"),
		MetricsHandler: sdktally.NewMetricsHandler(scope),
		Interceptors:   interceptors,
	})
	if err != nil {
		log.Fatalln("unable to connect to Temporal:", err)
	}
	defer c.Close()

	taskQueue := env("TASK_QUEUE", defaultTaskQueue)

	maxActivities := envInt("MAX_CONCURRENT_ACTIVITIES", 10)
	maxWorkflowTasks := envInt("MAX_CONCURRENT_WORKFLOW_TASKS", 10)

	// These two values are the difference between "we need more Workers" and
	// "our Workers are configured too small". Both look identical on the
	// schedule-to-start panel; only task_slots_available plus host CPU tells
	// them apart. The demo defaults are low on purpose so slot exhaustion is
	// reachable without generating serious load.
	w := worker.New(c, taskQueue, worker.Options{
		MaxConcurrentActivityExecutionSize:     maxActivities,
		MaxConcurrentWorkflowTaskExecutionSize: maxWorkflowTasks,
	})

	w.RegisterWorkflow(OrderWorkflow)
	w.RegisterActivity(ValidateOrder)
	w.RegisterActivity(ChargePayment)
	w.RegisterActivity(ShipOrder)

	// The agent Workflow is registered on every Worker, but only the Worker
	// polling AGENT_TASK_QUEUE will ever be handed one. Registering it
	// everywhere costs nothing and avoids the failure where a Worker is scaled
	// onto the agent queue and rejects tasks for a Workflow type it was built
	// with but never registered.
	w.RegisterWorkflow(AgentWorkflow)
	w.RegisterActivity(InvokeModel)
	w.RegisterActivity(RunAgentTool)
	w.RegisterActivity(ReportAgentRun)

	log.Printf("worker polling task queue %q (activity slots=%d, workflow task slots=%d)",
		taskQueue, maxActivities, maxWorkflowTasks)

	// worker.InterruptCh() is already SIGINT/SIGTERM on a <-chan interface{},
	// which is what Run wants. Feeding it a raw chan os.Signal does not compile.
	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalln("worker exited:", err)
	}
}
