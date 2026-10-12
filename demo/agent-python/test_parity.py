"""Parity tests: the Python worker must agree with the Go worker.

The dashboard's central claim is that a panel cannot tell which SDK produced a
number. Two independent implementations of the same contract will drift, and the
drift is silent — a cost panel that is right for Go and 10x wrong for Python
still renders, still trends, and still gets believed.

These cases are copied from ``demo/app/agent_test.go``. Keep them identical: if
one side changes, this fails, which is the entire point.

Run directly (``python test_parity.py``) or under pytest. Wired into
``make agent-py-test`` and checked by ``demo/scripts/validate.sh``.
"""

from __future__ import annotations

import sys

import telemetry

# (usage, expected USD) — mock-1 is $1/M input, $10/M output, $0.10/M cached read.
COST_CASES = [
    ({"input": 1_000_000, "output": 1_000_000}, 11.0, "input and output at full rate"),
    # The regression that matters: cached tokens are a SUBSET of input and are
    # billed cheaper, so they come off the full-rate portion. Adding instead of
    # subtracting gives 1.10 — a 10x overstatement on exactly the workloads that
    # optimised hardest.
    ({"input": 1_000_000, "cached": 1_000_000}, 0.10, "fully cached input"),
    ({"input": 1_000_000, "cached": 500_000}, 0.55, "half cached"),
    # Reasoning tokens are already inside output. Pricing them again would
    # double-bill every thinking model.
    ({"output": 1_000_000, "reasoning": 400_000}, 10.0, "reasoning not billed twice"),
    ({"input": 100, "cached": 500}, 0.00005, "cached exceeding input clamps to zero"),
]

ERROR_CASES = [
    ("HTTP 429: rate_limit_error", "rate_limit"),
    ("529 overloaded_error", "overloaded"),
    ("401 authentication_error", "auth"),
    ("400 invalid_request_error", "bad_request"),
    ("refusal: model declined", "refusal"),
    ("request timed out", "timeout"),
    ("503 service unavailable", "server_error"),
    ("something nobody anticipated", "unknown"),
]

# Every instrument and attribute name the Go worker emits. A rename on either
# side splits one series into two and halves every panel silently.
EXPECTED_INSTRUMENTS = {
    "gen_ai.client.token.usage",
    "gen_ai.client.operation.duration",
    "gen_ai.client.cost",
    "gen_ai.agent.tool.calls",
    "gen_ai.agent.loop.iterations",
    "gen_ai.agent.runs",
}

EXPECTED_ATTRIBUTES = {
    "gen_ai.system",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.operation.name",
    "gen_ai.token.type",
    "error.type",
    "gen_ai.tool.name",
    "gen_ai.agent.name",
    "outcome",
    "gen_ai.response.finish_reason",
}


def test_cost_parity() -> None:
    for usage, want, label in COST_CASES:
        got = telemetry.estimate_cost_usd("mock-1", usage)
        assert abs(got - want) < 1e-9, f"{label}: got {got}, want {want}"
        assert got >= 0, f"{label}: cost must never be negative"


def test_unpriced_model_contributes_zero() -> None:
    # A guess would put a confident wrong number on the dashboard; zero leaves a
    # visible hole that a human can fix in the pricing table.
    assert telemetry.estimate_cost_usd("some-model-nobody-priced", {"input": 1_000_000}) == 0.0


def test_error_taxonomy_parity() -> None:
    for message, want in ERROR_CASES:
        got = telemetry.classify_error(RuntimeError(message))
        assert got == want, f"{message!r}: got {got!r}, want {want!r}"


def test_instrument_names_match_go() -> None:
    declared = {
        telemetry.INSTR_TOKEN_USAGE,
        telemetry.INSTR_OP_DURATION,
        telemetry.INSTR_COST,
        telemetry.INSTR_TOOL_CALLS,
        telemetry.INSTR_LOOP_ITERATIONS,
        telemetry.INSTR_AGENT_RUNS,
    }
    assert declared == EXPECTED_INSTRUMENTS, f"instrument drift: {declared ^ EXPECTED_INSTRUMENTS}"


def test_attribute_names_match_go() -> None:
    declared = {
        telemetry.ATTR_SYSTEM,
        telemetry.ATTR_REQUEST_MODEL,
        telemetry.ATTR_RESPONSE_MODEL,
        telemetry.ATTR_OPERATION,
        telemetry.ATTR_TOKEN_TYPE,
        telemetry.ATTR_ERROR_TYPE,
        telemetry.ATTR_TOOL_NAME,
        telemetry.ATTR_AGENT_NAME,
        telemetry.ATTR_OUTCOME,
        telemetry.ATTR_STOP_REASON,
    }
    assert declared == EXPECTED_ATTRIBUTES, f"attribute drift: {declared ^ EXPECTED_ATTRIBUTES}"


def test_pricing_table_matches_go() -> None:
    # Prices are duplicated across two languages, which is a maintenance hazard
    # worth an explicit assertion rather than a comment asking people to be
    # careful.
    expected = {
        "claude-opus-5": (5.00, 25.00, 0.50),
        "claude-opus-4-8": (5.00, 25.00, 0.50),
        "claude-sonnet-5": (3.00, 15.00, 0.30),
        "claude-haiku-4-5": (1.00, 5.00, 0.10),
        "claude-fable-5": (10.00, 50.00, 1.00),
        "mock-1": (1.00, 10.00, 0.10),
    }
    got = {
        m: (p["input"], p["output"], p["cached_read"]) for m, p in telemetry.PRICING.items()
    }
    assert got == expected, f"pricing drift vs demo/app/agent_types.go: {got}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
