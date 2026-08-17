package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.temporal.io/sdk/contrib/opentelemetry"
	"go.temporal.io/sdk/interceptor"
)

// Tracing is OTel-only. Metrics stay on Tally.
//
// This is deliberate and it is the whole sequencing argument from
// docs/FR-otel-and-traces.md: the two exporters emit DIFFERENT METRIC NAMES
// (temporal_activity_schedule_to_start_latency_bucket under OTel versus
// ..._seconds_bucket under Tally). Switching metrics would rename every series
// this repo's rules and dashboards are built on — measured elsewhere in this
// repo as 16-18 permanently empty panels — for zero diagnostic gain. Traces
// answer a question metrics cannot; metrics answer questions traces should not
// be asked. Run both.

// initTracing wires an OTLP exporter and returns a shutdown func.
//
// Returns a no-op shutdown when OTEL_EXPORTER_OTLP_ENDPOINT is unset, so the
// stack runs unchanged without a collector. Tracing is additive here, never a
// startup dependency: a broken collector must not stop Workers from polling.
func initTracing(ctx context.Context, serviceName string) (func(context.Context) error, error) {
	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		log.Printf("INFO  tracing disabled (OTEL_EXPORTER_OTLP_ENDPOINT unset)")
		return func(context.Context) error { return nil }, nil
	}

	exp, err := otlptracegrpc.New(ctx,
		otlptracegrpc.WithEndpoint(endpoint),
		// Plain HTTP inside the compose network. Use TLS across any real boundary.
		otlptracegrpc.WithInsecure(),
		otlptracegrpc.WithTimeout(10*time.Second),
	)
	if err != nil {
		return nil, fmt.Errorf("otlp exporter: %w", err)
	}

	// NewSchemaless, not NewWithAttributes(semconv.SchemaURL, ...).
	//
	// resource.Default() carries whatever semconv schema URL the OTel SDK was
	// built against. Merging it with a resource declaring a DIFFERENT schema URL
	// fails with "conflicting Schema URL" — which is exactly what happened on
	// first run here, and it pins the app to the SDK's semconv version forever.
	// Schemaless attributes merge with anything.
	res, err := resource.Merge(resource.Default(), resource.NewSchemaless(
		semconv.ServiceName(serviceName),
		semconv.ServiceVersion("demo"),
		attribute.String("temporal.task_queue", env("TASK_QUEUE", "orders")),
	))
	if err != nil {
		return nil, fmt.Errorf("otel resource: %w", err)
	}

	// Sampling is the decision that decides whether this is affordable.
	//
	// AlwaysSample is right for a lab and wrong for production. Head sampling at
	// a low rate is the usual answer, but it drops slow executions at exactly the
	// same rate as fast ones — and the slow ones are the entire reason you are
	// looking. ParentBased keeps a trace whole once sampled, which matters more
	// here than elsewhere: a Temporal trace can span many Workers and hours.
	sampler := sdktrace.ParentBased(sdktrace.AlwaysSample())
	if ratio := os.Getenv("OTEL_TRACES_SAMPLER_ARG"); ratio != "" {
		var r float64
		if _, err := fmt.Sscanf(ratio, "%f", &r); err == nil && r >= 0 && r <= 1 {
			sampler = sdktrace.ParentBased(sdktrace.TraceIDRatioBased(r))
			log.Printf("INFO  trace sampling ratio %.3f", r)
		}
	}

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exp),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sampler),
	)
	otel.SetTracerProvider(tp)

	// Propagation must be set for context to cross the Workflow/Activity
	// boundary. Without it every Activity starts a NEW trace and the causal
	// chain — the only thing that makes this better than logs — is lost.
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{}, propagation.Baggage{},
	))

	log.Printf("INFO  tracing enabled service=%s endpoint=%s", serviceName, endpoint)
	return tp.Shutdown, nil
}

// tracingInterceptor propagates trace context across Workflow and Activity
// boundaries — the hard part of tracing a durable execution, since spans are
// separated by hours and by process restarts.
//
// ON REPLAY: the interceptor is replay-aware and does not re-emit spans for
// history that is being replayed. Verified empirically on this stack rather than
// taken from docs — see TESTING.md. It matters: a Workflow that replays 50 times
// would otherwise emit 50x the spans and make sampling maths meaningless.
func tracingInterceptor() (interceptor.ClientInterceptor, error) {
	return opentelemetry.NewTracingInterceptor(opentelemetry.TracerOptions{
		Tracer: otel.Tracer("temporal-obs-demo"),
	})
}
