# FR: Vanilla OpenTelemetry configuration and distributed traces

**Status:** proposed · **Owner:** TBD · **Raised:** 2026-08-14

## Why this is not just "another exporter"

Everything in this repo is metrics plus, recently, logs. Both answer *aggregate*
questions: how many, how often, how slow at p99. Neither answers the question an
on-call engineer actually asks first — **where did this one execution spend its
time?**

The stuck-workflow work made the shape of that gap concrete. `make chaos-stuck`
demonstrates executions that no metric can see, and `monitor/` closes half of
that by counting them. But counting is not diagnosis: the monitor can say *five
OrderWorkflows are past 2x budget* and never say *because the ChargePayment
Activity is blocked on a payment gateway that stopped responding*. A trace says
exactly that, and it is the natural third signal here rather than a nice-to-have.

## Scope

Three separable pieces. They are worth costing separately because the first is
nearly free and the third is not.

### 1. OTel metrics exporter alongside Tally (small)

The demo app wires the Go SDK through `uber-go/tally`. Temporal SDKs also support
an OpenTelemetry metrics handler.

**The catch, already measured in this repo:** the two emit **different metric
names**. `fetch-community-dashboards.sh` documents it — the OTel dashboards
render 16-18 panels permanently empty against a Tally exporter, because names
like `temporal_activity_schedule_to_start_latency_bucket` (OTel) and
`temporal_activity_schedule_to_start_latency_seconds_bucket` (Tally) are not the
same series.

So this is **not** a drop-in swap. Every rule and dashboard in `demo/`,
`production/` and `app-team/` is written against Tally names. Options:

- ship OTel as a documented *alternative* with its own rule/dashboard variants
  (honest, roughly doubles the surface to maintain), or
- migrate wholesale and regenerate everything from the `tools/` generators
  (cleaner end state, one-way door), or
- OTel for **traces only**, keep Tally for metrics (smallest change, and probably
  the right first step).

### 2. Traces from Workflows and Activities (medium)

Temporal ships interceptors that propagate trace context across Workflow and
Activity boundaries — which is the hard part of tracing a durable execution,
since a Workflow's spans are separated by hours and by process restarts.

Worth stating plainly in whatever we write: a Temporal trace is **not** a normal
request trace. A Workflow can outlive the trace backend's retention, replay
re-executes code that must not re-emit spans, and one execution can span many
Workers. Any doc that ignores this will produce traces that look wrong and get
distrusted.

### 3. Collector and backend (medium)

`otel-collector` in the compose stack, then a backend. Tempo is the obvious pick
here — Grafana-native, so traces land beside the existing Prometheus and Loki
datasources, and Grafana can link between all three.

The payoff is the correlation triangle we already have two thirds of:

- **Metric** says the SLI is burning.
- **Log** names the WorkflowID (already wired — derived fields link to the
  Temporal UI).
- **Trace** shows which Activity consumed the budget.

Loki's derived fields could link `WorkflowID` straight to a trace, which is
where this stops being three tools and starts being one workflow.

## Recommended sequencing

1. **Traces only, Tally untouched.** otel-collector + Tempo + SDK tracing
   interceptor in the demo app. No existing rule or dashboard changes, so the
   33/33 validation stays meaningful.
2. **Correlation.** Trace links in the Loki datasource; an exemplar or trace link
   on the golden-signals latency panels.
3. **Then decide on metrics.** Only if there is a real reason to leave Tally.
   Doing this first means rewriting every rule in the repo for no diagnostic gain.

## Open questions

1. **Sampling.** Head sampling will miss the slow executions we care most about;
   tail sampling needs collector state. What do we recommend for a Workflow that
   runs for a day?
2. **Replay.** Does the interceptor suppress spans during replay? If not, a
   Workflow that replays 50 times emits 50x the spans. This needs verifying
   empirically, not from docs — the same discipline that caught `failure_reason`
   vs `error_type`.
3. **Cost.** Traces are the most expensive signal per unit of insight. What
   sampling rate makes this affordable at the customer's volume?
4. **Cloud.** Temporal Cloud does not export traces for server-side processing;
   these would be Worker-side spans only. Worth setting that expectation early.

## Proposed first step

Timebox a spike on the demo stack: otel-collector + Tempo + tracing interceptor
in the Go app, then run `make chaos-backlog` and confirm a trace actually shows
schedule-to-start time as a visible gap. If the gap is visible in a span, this
earns its place. Then answer question 2, because it determines whether this is
affordable at all.
