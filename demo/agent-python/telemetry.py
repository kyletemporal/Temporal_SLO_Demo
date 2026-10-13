"""The same telemetry contract as the Go worker, in Python.

This file is deliberately close to a line-by-line mirror of
``demo/app/agent_telemetry.go``. That duplication is the point of the exercise:
if the two agree only by accident, the dashboard's central claim — that a panel
does not know which SDK produced a number — is not actually true.

The names come from the OpenTelemetry GenAI semantic conventions, not from this
repo. See the long note in the Go file for why that matters; the short version is
that ``gen_ai.client.token.usage`` is already emitted by the LangChain, OpenAI
and Anthropic auto-instrumentations, so a team that later drops this hand-rolled
code keeps its dashboard.

Any drift between this file and the Go one is caught by
``demo/scripts/validate.sh``, which asserts both processes produce the same
series names in Prometheus.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource

log = logging.getLogger(__name__)

# Instrument names — identical strings to the Go worker's constants.
INSTR_TOKEN_USAGE = "gen_ai.client.token.usage"
INSTR_OP_DURATION = "gen_ai.client.operation.duration"
INSTR_COST = "gen_ai.client.cost"
INSTR_TOOL_CALLS = "gen_ai.agent.tool.calls"
INSTR_LOOP_ITERATIONS = "gen_ai.agent.loop.iterations"
INSTR_AGENT_RUNS = "gen_ai.agent.runs"

# Attribute keys — likewise identical.
ATTR_SYSTEM = "gen_ai.system"
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_OPERATION = "gen_ai.operation.name"
ATTR_TOKEN_TYPE = "gen_ai.token.type"
ATTR_ERROR_TYPE = "error.type"
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_AGENT_NAME = "gen_ai.agent.name"
ATTR_OUTCOME = "outcome"
ATTR_STOP_REASON = "gen_ai.response.finish_reason"

# Same list prices as agentPricing in demo/app/agent_types.go, and the same
# caveat: these are PUBLISHED rates, not an invoice. A model that is absent
# contributes zero and leaves a visible hole in the cost panel, which is a
# better failure than a confident wrong number.
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cached_read": 0.50},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cached_read": 0.50},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00, "cached_read": 0.30},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cached_read": 0.10},
    "claude-fable-5": {"input": 10.00, "output": 50.00, "cached_read": 1.00},
    "mock-1": {"input": 1.00, "output": 10.00, "cached_read": 0.10},
}

_instruments: dict[str, Any] = {}


def init_metrics(service_name: str) -> None:
    """Wire the OTLP metric exporter. Fails open, like the Go worker.

    An unset endpoint disables reporting and leaves the agent running. A worker
    that refuses to poll because a collector is down is a worse outage than the
    missing telemetry.
    """
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        log.info("agent metrics disabled (OTEL_EXPORTER_OTLP_ENDPOINT unset)")
        return

    # The Go exporter takes a bare host:port; the Python one wants a URL scheme
    # and silently produces no data if given the bare form. Same collector, same
    # env var, two different expectations — normalise rather than document.
    if "://" not in endpoint:
        endpoint = f"http://{endpoint}"

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "demo",
            "temporal.task_queue": os.getenv("AGENT_TASK_QUEUE", "agents-langchain"),
        }
    )

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint, insecure=True),
        export_interval_millis=10_000,
    )

    # Same explicit boundaries as the Go worker. The SDK defaults top out at 10,
    # which puts every LLM call and every large prompt in the +Inf bucket and
    # makes every quantile on the dashboard wrong in the same direction.
    provider = MeterProvider(
        resource=resource,
        metric_readers=[reader],
        views=[
            View(
                instrument_name=INSTR_OP_DURATION,
                aggregation=ExplicitBucketHistogramAggregation(
                    boundaries=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300]
                ),
            ),
            View(
                instrument_name=INSTR_TOKEN_USAGE,
                aggregation=ExplicitBucketHistogramAggregation(
                    boundaries=[64, 256, 1024, 4096, 16384, 65536, 262144, 1048576]
                ),
            ),
        ],
    )
    metrics.set_meter_provider(provider)
    meter = metrics.get_meter("temporal-obs-demo/agent-langchain")

    # Units must match the Go worker exactly. The collector's Prometheus
    # exporter derives the series suffix from the unit, so a mismatch here
    # produces two differently-named series for the same logical metric and
    # quietly halves every panel.
    _instruments["tokens"] = meter.create_histogram(
        INSTR_TOKEN_USAGE,
        unit="{token}",
        description="Tokens consumed by a single model call, split by input/output/reasoning/cached.",
    )
    _instruments["duration"] = meter.create_histogram(
        INSTR_OP_DURATION,
        unit="s",
        description="Wall-clock duration of one model call, measured around the HTTP request only.",
    )
    _instruments["cost"] = meter.create_counter(
        INSTR_COST,
        unit="{USD}",
        description="Estimated spend in USD at published list prices.",
    )
    _instruments["tool_calls"] = meter.create_counter(
        INSTR_TOOL_CALLS,
        unit="{call}",
        description="Tool invocations requested by the model, by tool and outcome.",
    )
    _instruments["iterations"] = meter.create_histogram(
        INSTR_LOOP_ITERATIONS,
        unit="{iteration}",
        description="Model calls made before an agent run reached a final answer.",
    )
    _instruments["runs"] = meter.create_counter(
        INSTR_AGENT_RUNS,
        unit="{run}",
        description="Agent runs by terminal outcome.",
    )

    log.info("agent metrics enabled service=%s endpoint=%s", service_name, endpoint)


def classify_error(exc: BaseException) -> str:
    """Bucket an exception into a low-cardinality ``error.type``.

    Same taxonomy and same reasoning as ``classifyLLMError`` in Go: the buckets
    are chosen to match what an operator does next, and putting ``str(exc)`` on
    the label would mint a new time series per failing request.
    """
    msg = str(exc).lower()
    if "429" in msg or "rate_limit" in msg or "rate limit" in msg:
        return "rate_limit"
    if "529" in msg or "overloaded" in msg:
        return "overloaded"
    if "401" in msg or "403" in msg or "authentication" in msg or "permission" in msg:
        return "auth"
    if "timeout" in msg or "timed out" in msg or "deadline" in msg:
        return "timeout"
    if "refusal" in msg or "content_filter" in msg:
        return "refusal"
    if "400" in msg or "invalid_request" in msg:
        return "bad_request"
    if "500" in msg or "502" in msg or "503" in msg:
        return "server_error"
    return "unknown"


def estimate_cost_usd(model: str, usage: dict[str, int]) -> float:
    """Price one call. Mirrors ``estimateCostUSD``, including the cached-token
    subtraction — cached input is a SUBSET of input and is billed cheaper, so it
    comes off the full-rate portion rather than being added to it."""
    price = PRICING.get(model)
    if not price:
        return 0.0

    cached = usage.get("cached", 0)
    full_rate_input = max(usage.get("input", 0) - cached, 0)
    return (
        full_rate_input * price["input"]
        + cached * price["cached_read"]
        + usage.get("output", 0) * price["output"]
    ) / 1_000_000


def record_llm_call(
    *,
    provider: str,
    request_model: str,
    response_model: str,
    usage: dict[str, int],
    elapsed_s: float,
    error: BaseException | None = None,
) -> None:
    if not _instruments:
        return

    base = {
        ATTR_SYSTEM: provider,
        ATTR_REQUEST_MODEL: request_model,
        ATTR_OPERATION: "chat",
    }

    if error is not None:
        # error.type is set only on failure, matching the Go worker. Emitting it
        # as an empty string on success doubles the series count for nothing.
        _instruments["duration"].record(
            elapsed_s, {**base, ATTR_ERROR_TYPE: classify_error(error)}
        )
        return

    attrs = {**base, ATTR_RESPONSE_MODEL: response_model or request_model}
    _instruments["duration"].record(elapsed_s, attrs)

    for kind in ("input", "output", "reasoning", "cached"):
        n = usage.get(kind, 0)
        if n > 0:
            _instruments["tokens"].record(n, {**attrs, ATTR_TOKEN_TYPE: kind})

    cost = estimate_cost_usd(attrs[ATTR_RESPONSE_MODEL], usage)
    if cost > 0:
        _instruments["cost"].add(cost, attrs)


def record_tool_call(tool: str, outcome: str) -> None:
    if not _instruments:
        return
    _instruments["tool_calls"].add(1, {ATTR_TOOL_NAME: tool, ATTR_OUTCOME: outcome})


def record_agent_run(agent_name: str, outcome: str, stop_reason: str, iterations: int) -> None:
    if not _instruments:
        return
    attrs = {
        ATTR_AGENT_NAME: agent_name,
        ATTR_OUTCOME: outcome,
        ATTR_STOP_REASON: stop_reason,
    }
    _instruments["runs"].add(1, attrs)
    _instruments["iterations"].record(iterations, attrs)
