# Observing AI agents on Temporal

An agent fleet fails in ways a Temporal dashboard cannot see. This document is
about those ways, the telemetry that catches them, and the two architectures
you can build an agent with — which differ in cost, in recoverability, and in
what their telemetry looks like.

Everything here runs with **no API key and no egress**. The default provider is
an offline mock with realistic token counts, tool-calling behaviour and latency,
so `make agent-demo` populates every panel on a laptop in an aeroplane.

---

## 1. The three failures that move no Temporal metric

This repo's golden-signals board measures whether work is flowing: latency,
error rate, saturation, queue depth. Agent Workflows appear on it like any
other. But the three ways an agent fleet actually goes wrong are all invisible
there, and all three are invisible for the same reason — **none of them is an
error**.

### It loops

An agent that calls a tool, gets a result it cannot use, and calls it again
will keep going until something stops it. Every iteration is a full-price model
call. Meanwhile:

| Signal | What it does |
|---|---|
| `workflow_failed` | nothing — the Workflow succeeds |
| Activity failure rate | nothing — every Activity returns cleanly |
| Retry count | nothing — nothing is retried |
| Schedule-to-start | nothing — the queue is keeping up fine |
| **Token spend** | **climbs linearly** |
| **`gen_ai_agent_loop_iterations`** | **climbs to the cap** |

When the iteration cap finally stops it, Temporal records a **successful**
Workflow that returned a result and answered nothing.

Run it: `make agent-runaway`. Watch `Run outcomes` split off a
`max_iterations` series while every panel on the golden-signals board stays
flat.

This is the agent analogue of `make chaos-stuck` — scenario 6, where a parked
Workflow is healthy in every dimension the platform measures and only its
*duration* is wrong. Here only its *cost* is wrong.

### It gets more expensive without getting slower

A prompt change, a model swap, or a cache that stopped hitting. Latency is
unchanged, error rate is unchanged, throughput is unchanged, and the monthly
bill is 3x.

The **cache hit ratio** is where this usually lives. Prompt caching is a
*prefix* match: a timestamp, a UUID, or a per-user string interpolated anywhere
near the front of the prompt invalidates everything after it. Cached input
bills at roughly a tenth of the normal rate, so losing the cache roughly triples
input cost — silently, with nothing broken.

`Cost per run` is the panel that catches this, because it is the one figure
that survives a change in traffic. Total spend rising with volume is expected;
cost per run rising is a regression.

### It quietly stops using a tool

A prompt edit weakens the instruction to use a tool; the model stops calling
it; answers get worse. **No technical signal moves at all** — not latency, not
errors, not cost. The `Tool calls by tool and outcome` panel is the only place
this appears, as a line going to zero.

`GenAIToolUsageStopped` alerts on the extreme case.

---

## 2. The telemetry contract

Everything is emitted under the **OpenTelemetry GenAI semantic conventions**
rather than names invented here. That is a deliberate bet: `gen_ai.client.
token.usage` is already emitted by the OpenAI, Anthropic and LangChain
auto-instrumentations, by LiteLLM, and by the collector's own GenAI processors.
A team that later replaces this hand-rolled instrumentation with an
auto-instrumentation keeps its dashboard.

### Series names

**Read off a running collector, not derived.** The OTel Prometheus exporter
builds a series name from the instrument name *and its unit*, and the unit rules
are not what you would guess:

| Instrument | Unit | Exported series |
|---|---|---|
| `gen_ai.client.operation.duration` | `s` | `gen_ai_client_operation_duration_seconds` |
| `gen_ai.client.token.usage` | `{token}` | `gen_ai_client_token_usage` |
| `gen_ai.client.cost` | `{USD}` | `gen_ai_client_cost_total` |
| `gen_ai.agent.tool.calls` | `{call}` | `gen_ai_agent_tool_calls_total` |
| `gen_ai.agent.loop.iterations` | `{iteration}` | `gen_ai_agent_loop_iterations` |
| `gen_ai.agent.runs` | `{run}` | `gen_ai_agent_runs_total` |

`s` becomes a `_seconds` suffix. An **annotation unit** — anything in braces —
is dropped entirely. So the token histogram has *no* unit suffix while duration
has one. Writing `..._token_usage_tokens` from the naming pattern gives a
permanently empty panel that looks exactly like "no agents have run".

### Attributes

| Attribute | On | Notes |
|---|---|---|
| `gen_ai.system` | token, duration, cost | provider: `anthropic`, `openai`, `mock` |
| `gen_ai.request.model` | token, duration, cost | what was asked for |
| `gen_ai.response.model` | token, duration, cost | what actually served it — differs on a fallback |
| `gen_ai.token.type` | token | `input` / `output` / `reasoning` / `cached` |
| `error.type` | duration | **only present on failures** |
| `gen_ai.tool.name`, `outcome` | tool calls | |
| `gen_ai.agent.name`, `outcome`, `gen_ai.response.finish_reason` | runs, iterations | |

**`reasoning` is a subset of `output`, and `cached` is a subset of `input`.**
Summing all four double-counts. They are reported separately because each
answers a question the totals cannot: reasoning tokens are billed as output but
never appear in the response body, so a bill that grows while visible output
does not is otherwise unattributable.

### Why OTel metrics here when the rest of the repo is on Tally

`tracing.go` argues at length for keeping Temporal SDK metrics on Tally: the two
exporters emit **different names** for the same Temporal series, and switching
would empty every existing panel. That argument is about a name *collision*, and
it does not apply here — nothing in the Temporal SDK emits `gen_ai.*`.

The consequence is visible in `prometheus.yml`: a Worker is **two scrape
targets**. Tally series are scraped from the app on `:8077`; `gen_ai` series go
OTLP to the collector and are scraped from its Prometheus exporter on `:8889`.

One thing to know before debugging: `up` for the `otel-collector-genai` job says
whether the *collector* is reachable, not whether any agent is running. An agent
Worker that has crashed leaves that target up and its series flat. The
`temporal-sdk-agent-worker` job is what tells you a Worker went away.

---

## 3. Two architectures, and the cost difference

The repo ships the same agent twice, deliberately.

### Loop in the Workflow (Go — `demo/app/agent_workflow.go`)

Each model call is its own Activity. The loop counter and the conversation live
in Workflow history.

```
AgentWorkflow
 ├── InvokeModel      (Activity)  ← retryable, one model call
 ├── RunAgentTool     (Activity)  ← parallel across tool calls
 ├── InvokeModel      (Activity)
 └── ReportAgentRun   (Activity)
```

### Loop inside one Activity (Python/LangChain — `demo/agent-python/agent.py`)

`create_agent` owns its own loop. Temporal sees one opaque Activity.

```
LangChainAgentWorkflow
 └── run_langchain_agent  (Activity)  ← the ENTIRE loop
```

### What that costs you

| | Loop in Workflow | Loop in Activity |
|---|---|---|
| Worker crash at iteration 7 | resumes at 7 | **re-runs from 1, re-paying every token** |
| Retry after a transient 529 | re-pays for **one call** | re-pays for **the whole loop** |
| Retry policy that makes sense | 3 attempts | **1 attempt** |
| Progress visibility | per-iteration Activity spans | nothing until it returns |
| Code you write | the loop | a prompt |

The retry row is the one that costs money. The LangChain Workflow is configured
`maximum_attempts=1` for exactly this reason: whether to spend the money twice is
a business decision, not a default worth inheriting.

**Both emit the identical telemetry contract.** The architectural difference
shows up as a different *shape* in the same panels — not as a second dashboard.
`demo/agent-python/test_parity.py` asserts the two agree on instrument names,
attribute names, the pricing table and the error taxonomy; `make agent-py-test`
runs it.

---

## 4. Providers

Three implementations of one interface, in `demo/app/`:

| Provider | File | Needs |
|---|---|---|
| `mock` (default) | `agent_activities.go` | nothing — offline, deterministic |
| `anthropic` | `agent_anthropic.go` | `ANTHROPIC_API_KEY` |
| `openai` | `agent_openai.go` | `OPENAI_API_KEY`, or `OPENAI_BASE_URL` for Azure/vLLM/Ollama/LiteLLM |

Reading `agent_anthropic.go` beside `agent_openai.go` is the point of having
both. Eight things differ for one logical call:

| | Anthropic | OpenAI |
|---|---|---|
| tool results | content block in a **user** message | message with role `tool` |
| tool arguments | decoded object | JSON string |
| token fields | `input` / `output` | `prompt` / `completion` |
| reasoning tokens | `output_tokens_details.thinking_tokens` | `completion_tokens_details.reasoning_tokens` |
| cached input | `cache_read_input_tokens` | `prompt_tokens_details.cached_tokens` |
| system prompt | top-level field | first message |
| effort | `output_config.effort` | `reasoning_effort` |
| `max_tokens` | **required** | optional |

None of that reaches the Workflow, the metrics, or the dashboard. That is what
normalising at the edge buys.

Two provider-specific traps worth knowing, both of which return **HTTP 200**:

- **Anthropic refusals** arrive as `stop_reason: "refusal"`. Code that reads
  `content[0]` unconditionally breaks; code that only checks `err` treats a
  refusal as an empty answer.
- **OpenAI content filtering** arrives as `finish_reason: "content_filter"`.

Both map into the same `refusal` bucket, which is what lets one alert cover both.

---

## 5. Cost figures are estimates

The cost panels are **list-price estimates**. They will not match an invoice:
negotiated discounts, committed-use terms, batch pricing at half rate and free
tiers all move the real figure. Read them as a trend and an attribution tool —
is spend rising, and which model is doing it — never as a finance source of
truth.

**A model with no entry in the pricing table contributes zero.** That is
deliberate: a confident wrong number on a cost dashboard is worse than a visible
gap, because someone will budget against it. The `Unpriced models` panel exists
to make that gap visible; **empty is the healthy state**.

To add a model, update **both**:

- `agentPricing` in `demo/app/agent_types.go`
- `PRICING` in `demo/agent-python/telemetry.py`

`make agent-py-test` fails if they disagree.

---

## 6. Running it

```bash
make up                  # includes the Go agent worker on task queue "agents"
make agent               # one run, prints the result
make agent-demo          # ~29 runs — populates every panel
make agent-runaway       # the loop that no Temporal metric sees
make dashboards-check    # assert every panel query returns data
```

Board: <http://localhost:3000/d/ai-agent-observability>

Real providers:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> demo/.env
docker compose up -d agent-worker
make agent PROVIDER=anthropic
```

LangChain worker (opt-in — large image, and **no offline mock**, so it needs a
key or it crash-loops):

```bash
make agent-langchain
```

---

## 7. Alerts

In `demo/prometheus/agent-rules.yml`. **Every threshold is a placeholder** —
agent cost and loop depth are entirely workload-specific, and an $8/hour fleet
is either a rounding error or a crisis depending on whose it is. Tune against
two weeks of your own baseline.

| Alert | Severity | Catches |
|---|---|---|
| `GenAIAgentRunawayLoops` | warning | >10% of runs hitting the cap |
| `GenAISpendRateHigh` | warning | spend rate, not a monthly total — fires while the money is being spent |
| `GenAICacheHitRatioCollapsed` | **info** | a prompt-prefix change that disabled caching |
| `GenAIProviderErrorsHigh` | warning | >5% of calls failing, **excluding refusals** |
| `GenAIRefusalsElevated` | **info** | >2% refused — a prompt problem, not an outage |
| `GenAIToolUsageStopped` | warning | agents running, no tools called |

Two severity choices are deliberate:

- **Cache collapse is `info`.** Nothing is broken and nobody should be woken up.
  Giving a cost finding a paging severity is how cost alerts get muted
  permanently.
- **Refusals are excluded from the error-rate alert.** A refusal is a content
  decision, not a fault — the provider worked correctly and declined. Counting
  it as an availability error means one badly-worded prompt template pages
  someone at 3am for a system functioning exactly as designed.

---

## 8. What is not here

- **No prompt or completion text on any metric.** The GenAI conventions define
  optional attributes carrying prompt content. On a span that is a defensible
  trade; on a metric it is unbounded cardinality made of customer data — every
  distinct prompt becomes a permanent time series. The collector deletes them
  (`attributes/genai_redact`), so no SDK and no future Worker can leak them by
  default.
- **No quality or eval metrics.** Whether the answers are any *good* is not
  something this telemetry can see. Token spend, loop depth and tool usage are
  proxies for behaviour, not for correctness. An agent that loops twice as much
  and answers better is an improvement this board will report as a regression —
  only an eval suite can tell you which it was.
- **No per-user or per-tenant attribution.** It would be the obvious next
  addition and it is a cardinality decision, not a code one: a `tenant` label on
  the cost counter is fine for fifty tenants and ruinous for fifty thousand.
