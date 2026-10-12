package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
)

// THE AGENT TELEMETRY CONTRACT
//
// Everything in this file exists to make one claim true: the dashboard does not
// know which LLM SDK produced a number. Anthropic, OpenAI and LangChain emit the
// same instrument names with the same attribute keys, so a panel is written once
// and a fourth provider costs nothing but an attribute value.
//
// The names come from the OpenTelemetry GenAI semantic conventions rather than
// from this repo, deliberately. An invented name is a name only this dashboard
// can read; gen_ai.client.token.usage is already emitted by the OpenAI, Anthropic
// and LangChain auto-instrumentations, by LiteLLM, and by the collector's own
// GenAI processors. Adopting the convention means a team that later swaps this
// hand-rolled instrumentation for an auto-instrumentation keeps its dashboard.
//
// WHY OTEL METRICS HERE WHEN THE REST OF THE REPO IS ON TALLY
//
// tracing.go argues at length for keeping Temporal SDK metrics on Tally: the two
// exporters emit DIFFERENT NAMES for the same Temporal series, and switching
// would empty every existing panel. That argument is about a name COLLISION, and
// it does not apply here. Nothing in the Temporal SDK emits gen_ai.*, so this is
// a disjoint namespace living beside the Tally one, not a replacement for it.
//
// The two reach Prometheus by different routes and that is visible in
// prometheus.yml: Tally series are scraped from the app on :8077, gen_ai series
// go OTLP to the collector and are scraped from its Prometheus exporter on :8889.
// A worker therefore appears as two scrape targets. That is intentional — it is
// also the honest picture of what a real mixed-instrumentation fleet looks like.

const (
	// Semantic-convention instruments. Do not rename these to something
	// prettier; their value is that they are not ours.
	instrTokenUsage = "gen_ai.client.token.usage"
	instrOpDuration = "gen_ai.client.operation.duration"

	// No semantic convention exists for these yet. They are prefixed to sit
	// alongside the conventional ones without pretending to be them, so that if
	// the working group later standardises a name we can migrate one metric
	// rather than un-pick a namespace.
	instrCost           = "gen_ai.client.cost"
	instrToolCalls      = "gen_ai.agent.tool.calls"
	instrLoopIterations = "gen_ai.agent.loop.iterations"
	instrAgentRuns      = "gen_ai.agent.runs"
)

// Attribute keys, also from the GenAI conventions.
//
// gen_ai.system is the provider ("anthropic", "openai", "mock"). It is the one
// attribute that must never be dropped: every cost, latency and token panel
// splits on it, and a fleet that reports a blended figure across providers can
// tell you that spend rose without telling you which model did it.
const (
	attrSystem        = "gen_ai.system"
	attrRequestModel  = "gen_ai.request.model"
	attrResponseModel = "gen_ai.response.model"
	attrOperation     = "gen_ai.operation.name"
	attrTokenType     = "gen_ai.token.type"
	attrErrorType     = "error.type"
	attrToolName      = "gen_ai.tool.name"
	attrAgentName     = "gen_ai.agent.name"
	attrOutcome       = "outcome"
	attrStopReason    = "gen_ai.response.finish_reason"
)

// agentMetrics is the whole instrument set, created once per process.
//
// Held as a struct rather than package-level vars so that a nil check has one
// place to live: when OTEL_EXPORTER_OTLP_ENDPOINT is unset the agent still runs,
// it just does not report. Observability that refuses to start the application
// is worse than none — the same rule tracing.go and profiling.go already follow.
type agentMetrics struct {
	tokens     metric.Int64Histogram
	duration   metric.Float64Histogram
	cost       metric.Float64Counter
	toolCalls  metric.Int64Counter
	iterations metric.Int64Histogram
	runs       metric.Int64Counter
}

var agentMeters *agentMetrics

// initAgentMetrics wires an OTLP metric exporter and builds the instruments.
//
// Returns a no-op shutdown when the endpoint is unset, matching initTracing.
func initAgentMetrics(ctx context.Context, serviceName string) (func(context.Context) error, error) {
	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		log.Printf("INFO  agent metrics disabled (OTEL_EXPORTER_OTLP_ENDPOINT unset)")
		return func(context.Context) error { return nil }, nil
	}

	exp, err := otlpmetricgrpc.New(ctx,
		otlpmetricgrpc.WithEndpoint(endpoint),
		otlpmetricgrpc.WithInsecure(),
		otlpmetricgrpc.WithTimeout(10*time.Second),
	)
	if err != nil {
		return nil, fmt.Errorf("otlp metric exporter: %w", err)
	}

	// NewSchemaless for the same reason tracing.go uses it: merging a resource
	// that declares a different semconv schema URL than resource.Default()
	// carries fails outright with "conflicting Schema URL".
	res, err := resource.Merge(resource.Default(), resource.NewSchemaless(
		semconv.ServiceName(serviceName),
		semconv.ServiceVersion("demo"),
		attribute.String("temporal.task_queue", env("AGENT_TASK_QUEUE", defaultAgentTaskQueue)),
	))
	if err != nil {
		return nil, fmt.Errorf("otel metric resource: %w", err)
	}

	// A 10s period against a 10s scrape interval. Longer would mean the
	// dashboard lags a burst of spend; shorter just resends the same cumulative
	// values more often.
	reader := sdkmetric.NewPeriodicReader(exp, sdkmetric.WithInterval(10*time.Second))

	// Explicit bucket boundaries on both histograms.
	//
	// The SDK default boundaries top out at 10 — fine for seconds of HTTP
	// latency, useless for either of these. An LLM call routinely takes 30s at
	// high effort and a single request can carry 100k tokens, so with the
	// defaults every interesting value lands in the +Inf bucket and every
	// quantile the dashboard computes is a lie in the same direction.
	provider := sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(res),
		sdkmetric.WithReader(reader),
		sdkmetric.WithView(sdkmetric.NewView(
			sdkmetric.Instrument{Name: instrOpDuration},
			sdkmetric.Stream{Aggregation: sdkmetric.AggregationExplicitBucketHistogram{
				Boundaries: []float64{0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300},
			}},
		)),
		sdkmetric.WithView(sdkmetric.NewView(
			sdkmetric.Instrument{Name: instrTokenUsage},
			sdkmetric.Stream{Aggregation: sdkmetric.AggregationExplicitBucketHistogram{
				Boundaries: []float64{64, 256, 1024, 4096, 16384, 65536, 262144, 1048576},
			}},
		)),
	)
	otel.SetMeterProvider(provider)

	m := provider.Meter("temporal-obs-demo/agent")
	built := &agentMetrics{}

	// Every instrument carries a unit. The collector's Prometheus exporter
	// derives the series suffix from it, so an omitted unit here is a renamed
	// series downstream and a permanently empty panel.
	if built.tokens, err = m.Int64Histogram(instrTokenUsage,
		metric.WithDescription("Tokens consumed by a single model call, split by input/output/reasoning/cached."),
		metric.WithUnit("{token}"),
	); err != nil {
		return nil, err
	}
	if built.duration, err = m.Float64Histogram(instrOpDuration,
		metric.WithDescription("Wall-clock duration of one model call, measured around the HTTP request only."),
		metric.WithUnit("s"),
	); err != nil {
		return nil, err
	}
	if built.cost, err = m.Float64Counter(instrCost,
		metric.WithDescription("Estimated spend in USD at published list prices. See agentPricing for the caveats."),
		metric.WithUnit("{USD}"),
	); err != nil {
		return nil, err
	}
	if built.toolCalls, err = m.Int64Counter(instrToolCalls,
		metric.WithDescription("Tool invocations requested by the model, by tool and outcome."),
		metric.WithUnit("{call}"),
	); err != nil {
		return nil, err
	}
	if built.iterations, err = m.Int64Histogram(instrLoopIterations,
		metric.WithDescription("Model calls made before an agent run reached a final answer."),
		metric.WithUnit("{iteration}"),
	); err != nil {
		return nil, err
	}
	if built.runs, err = m.Int64Counter(instrAgentRuns,
		metric.WithDescription("Agent runs by terminal outcome."),
		metric.WithUnit("{run}"),
	); err != nil {
		return nil, err
	}

	agentMeters = built
	log.Printf("INFO  agent metrics enabled service=%s endpoint=%s", serviceName, endpoint)
	return provider.Shutdown, nil
}

// recordLLMCall reports one model call. Called from the Activity, never from the
// Workflow: a Workflow must be deterministic on replay, and a metric emitted
// there would be re-emitted on every replay and silently multiply the fleet's
// reported token spend by the replay count.
func recordLLMCall(ctx context.Context, r LLMReply, provider, model string, elapsed time.Duration, callErr error) {
	if agentMeters == nil {
		return
	}

	base := []attribute.KeyValue{
		attribute.String(attrSystem, provider),
		attribute.String(attrRequestModel, model),
		attribute.String(attrOperation, "chat"),
	}

	if callErr != nil {
		// error.type is set ONLY on failure. Emitting error.type="" on the
		// success path doubles the series count for no information and makes
		// every rate() query carry a label matcher it does not need.
		agentMeters.duration.Record(ctx, elapsed.Seconds(),
			metric.WithAttributes(append(base, attribute.String(attrErrorType, classifyLLMError(callErr)))...))
		return
	}

	respModel := r.Model
	if respModel == "" {
		respModel = model
	}
	success := append(base, attribute.String(attrResponseModel, respModel))

	agentMeters.duration.Record(ctx, elapsed.Seconds(), metric.WithAttributes(success...))

	tok := func(kind string, n int64) {
		if n <= 0 {
			return
		}
		agentMeters.tokens.Record(ctx, n, metric.WithAttributes(
			append(success, attribute.String(attrTokenType, kind))...))
	}
	tok("input", r.Usage.InputTokens)
	tok("output", r.Usage.OutputTokens)

	// Reasoning and cached tokens are reported SEPARATELY and are already
	// counted inside input/output. Summing all four double-counts.
	//
	// They are worth their own series anyway. Reasoning tokens are billed as
	// output but are not in the response body, so a bill that grows while
	// visible output does not is otherwise unattributable; cached input tokens
	// bill at roughly a tenth of the rate, so cache hit ratio is the single
	// biggest lever on spend and it is invisible without this split.
	tok("reasoning", r.Usage.ReasoningTokens)
	tok("cached", r.Usage.CachedInputTokens)

	if cost := estimateCostUSD(provider, respModel, r.Usage); cost > 0 {
		agentMeters.cost.Add(ctx, cost, metric.WithAttributes(success...))
	}
}

func recordToolCall(ctx context.Context, tool, outcome string) {
	if agentMeters == nil {
		return
	}
	agentMeters.toolCalls.Add(ctx, 1, metric.WithAttributes(
		attribute.String(attrToolName, tool),
		attribute.String(attrOutcome, outcome),
	))
}

// recordAgentRun closes out a run. Called from the terminal Activity rather than
// from the Workflow, for the replay reason above.
func recordAgentRun(ctx context.Context, agentName, outcome, stopReason string, iterations int) {
	if agentMeters == nil {
		return
	}
	attrs := []attribute.KeyValue{
		attribute.String(attrAgentName, agentName),
		attribute.String(attrOutcome, outcome),
		attribute.String(attrStopReason, stopReason),
	}
	agentMeters.runs.Add(ctx, 1, metric.WithAttributes(attrs...))
	agentMeters.iterations.Record(ctx, int64(iterations), metric.WithAttributes(attrs...))
}
