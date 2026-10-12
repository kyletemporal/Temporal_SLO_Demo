"""LangChain agent, run inside a Temporal Activity.

READ THIS BEFORE COPYING THE PATTERN — it is deliberately the OTHER shape.

``demo/app/agent_workflow.go`` puts the agent loop in the Workflow: each model
call is its own Activity, so the loop counter and the conversation live in
Workflow history and a Worker crash at iteration seven resumes at iteration
seven.

This file does the opposite, because it is what using LangChain actually looks
like. ``create_agent`` owns its own loop; the whole thing runs inside ONE
Activity, and Temporal sees an opaque call that either returns or does not.

The trade is real and it is visible in the telemetry, which is why both exist:

    Go path            LangChain path
    ---------------------------------------------------------------
    loop in Workflow   loop inside one Activity
    crash resumes at   crash re-runs the WHOLE loop, re-paying for
    the last iteration every token already spent
    per-iteration      one Activity span covering everything; the
    Activity spans     per-iteration detail comes from the callback
    heartbeat-able     no progress visible until it returns

The second row is the one that costs money. A LangChain agent Activity that
fails on its ninth model call and retries does not resume — it starts again at
call one, and the retry policy in workflow.py is set to 1 attempt for exactly
that reason. Retrying is a business decision about spending the money twice, not
a default worth inheriting.

The point of shipping both is that the DASHBOARD IS THE SAME. Same metric names,
same attributes, same panels — the architectural difference shows up as a
different shape in the data, not as a second dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from temporalio import activity

import telemetry

log = logging.getLogger(__name__)

AGENT_NAME = "order-assistant"
SYSTEM_PROMPT = """You are a logistics assistant for an order system.
Use the provided tools to answer questions about orders, inventory and shipping.
Call a tool when you need a fact you do not have; answer directly when you do.
Keep answers to two sentences."""


@dataclass
class AgentRequest:
    prompt: str
    provider: str = "anthropic"
    model: str = ""
    max_iterations: int = 8
    fail_tool_rate: float = 0.0


@dataclass
class AgentReply:
    answer: str = ""
    iterations: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = ""


# ---------------------------------------------------------------------------
# Tools — the same three the Go worker exposes, with the same names, so the
# tool-call panel aggregates across both languages instead of splitting.
# ---------------------------------------------------------------------------

_fail_tool_rate = 0.0


@tool
def lookup_order(order_id: str) -> str:
    """Look up the status and contents of an order by its ID."""
    _maybe_fail("lookup_order")
    telemetry.record_tool_call("lookup_order", "ok")
    return json.dumps(
        {"order_id": order_id, "status": "in_transit", "items": 2, "sku": "SKU-114"}
    )


@tool
def check_inventory(sku: str) -> str:
    """Check warehouse stock for a SKU."""
    _maybe_fail("check_inventory")
    # Deliberately slow, matching the Go tool. An agent's latency is usually
    # dominated by its tools rather than by the model, and a flat tool-latency
    # panel hides that entirely.
    time.sleep(0.25)
    telemetry.record_tool_call("check_inventory", "ok")
    return json.dumps({"sku": sku, "on_hand": 47, "reserved": 6, "warehouse": "LHR-2"})


@tool
def quote_shipping(destination: str, weight_kg: float = 1.0) -> str:
    """Quote a shipping cost and ETA to a destination."""
    _maybe_fail("quote_shipping")
    telemetry.record_tool_call("quote_shipping", "ok")
    return json.dumps({"destination": destination, "cost_gbp": 6.40, "eta_days": 3})


def _maybe_fail(name: str) -> None:
    """Injected tool failure. Raises so LangChain hands the error back to the
    model as a tool message — the model then sees the failure and adapts, and
    the adaptation costs tokens. That extra spend is the thing worth watching."""
    if _fail_tool_rate > 0 and random.random() < _fail_tool_rate:
        telemetry.record_tool_call(name, "error")
        raise RuntimeError(f"tool {name} is unavailable")


TOOLS = [lookup_order, check_inventory, quote_shipping]


# ---------------------------------------------------------------------------
# Telemetry callback
#
# LangChain owns the loop, so the only way to see inside it is its callback
# interface. Without this the whole agent is a single opaque Activity and the
# per-call token and latency panels stay empty for the Python worker while the
# Go worker populates them — which would look like a dashboard bug rather than
# an instrumentation gap.
# ---------------------------------------------------------------------------


class GenAITelemetryCallback(AsyncCallbackHandler):
    def __init__(self, provider: str, request_model: str) -> None:
        self.provider = provider
        self.request_model = request_model
        self.iterations = 0
        self.totals: dict[str, int] = {"input": 0, "output": 0, "reasoning": 0, "cached": 0}
        self._started: dict[str, float] = {}

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs) -> None:
        self._started[str(run_id)] = time.perf_counter()

    async def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        elapsed = time.perf_counter() - self._started.pop(str(run_id), time.perf_counter())
        self.iterations += 1

        usage = {"input": 0, "output": 0, "reasoning": 0, "cached": 0}
        response_model = self.request_model

        for generations in response.generations:
            for gen in generations:
                message = getattr(gen, "message", None)
                if message is None:
                    continue

                # usage_metadata is LangChain's normalised shape and it maps
                # one-to-one onto the Go worker's TokenUsage — which is a useful
                # independent check that the contract is not Go-specific.
                meta = getattr(message, "usage_metadata", None) or {}
                usage["input"] += meta.get("input_tokens", 0) or 0
                usage["output"] += meta.get("output_tokens", 0) or 0
                usage["reasoning"] += (meta.get("output_token_details") or {}).get("reasoning", 0) or 0
                usage["cached"] += (meta.get("input_token_details") or {}).get("cache_read", 0) or 0

                rm = (getattr(message, "response_metadata", None) or {}).get("model_name")
                if rm:
                    response_model = rm

        for k, v in usage.items():
            self.totals[k] += v

        telemetry.record_llm_call(
            provider=self.provider,
            request_model=self.request_model,
            response_model=response_model,
            usage=usage,
            elapsed_s=elapsed,
        )

    async def on_llm_error(self, error, *, run_id, **kwargs) -> None:
        elapsed = time.perf_counter() - self._started.pop(str(run_id), time.perf_counter())
        telemetry.record_llm_call(
            provider=self.provider,
            request_model=self.request_model,
            response_model=self.request_model,
            usage={},
            elapsed_s=elapsed,
            error=error,
        )


# ---------------------------------------------------------------------------
# The Activity
# ---------------------------------------------------------------------------


def _build_model(provider: str, model: str):
    """Late import so the Temporal Workflow sandbox never sees these modules.

    LangChain pulls in a large dependency tree with module-level state; importing
    it at Workflow scope is the usual cause of sandbox restriction errors in a
    Temporal Python worker.
    """
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # No temperature. It is removed on Claude Opus 5 and any value returns a
        # 400 — the same constraint the Go provider documents.
        return ChatAnthropic(model=model or "claude-opus-5", max_tokens=4096)

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))

    raise ValueError(f"bad_request: unknown provider {provider!r}")


@activity.defn
async def run_langchain_agent(req: AgentRequest) -> AgentReply:
    global _fail_tool_rate
    _fail_tool_rate = req.fail_tool_rate

    provider = req.provider or "anthropic"
    model = req.model or ("claude-opus-5" if provider == "anthropic" else os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))

    callback = GenAITelemetryCallback(provider, model)

    try:
        from langchain.agents import create_agent

        agent = create_agent(
            _build_model(provider, model),
            TOOLS,
            system_prompt=SYSTEM_PROMPT,
        )

        # recursion_limit is LangGraph's loop cap and it is the direct analogue
        # of MaxIterations in the Go Workflow. An agent loop without one runs
        # until something else stops it, and every turn is billed. Doubled
        # because LangGraph counts model and tool steps as separate nodes.
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=req.prompt)]},
            config={"callbacks": [callback], "recursion_limit": req.max_iterations * 2},
        )

        answer = ""
        for message in reversed(result.get("messages", [])):
            if isinstance(message, AIMessage) and not message.tool_calls:
                answer = message.text() if callable(getattr(message, "text", None)) else str(message.content)
                break

        telemetry.record_agent_run(AGENT_NAME, "completed", "end_turn", callback.iterations)
        return AgentReply(
            answer=answer,
            iterations=callback.iterations,
            usage=callback.totals,
            stop_reason="end_turn",
        )

    except Exception as exc:  # noqa: BLE001 — classified, re-raised below
        kind = telemetry.classify_error(exc)

        # A recursion-limit hit is the runaway-agent case and is NOT an error in
        # the same sense as a 500: nothing broke, the agent simply never
        # converged and spent the whole budget getting there. It gets its own
        # outcome so the dashboard can separate "the provider failed us" from
        # "we paid for a loop that went nowhere".
        outcome = "max_iterations" if "recursion" in str(exc).lower() else "error"
        telemetry.record_agent_run(AGENT_NAME, outcome, kind, callback.iterations)

        activity.logger.warning(
            "langchain agent failed", extra={"error_type": kind, "iterations": callback.iterations}
        )
        raise
