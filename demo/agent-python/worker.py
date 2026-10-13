"""Temporal Python Worker for the LangChain agent.

Runs on its own task queue (``agents-langchain``) beside the Go agent Worker on
``agents``. Two languages, three LLM SDKs, one dashboard — that separation is
what makes the claim testable rather than asserted.
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

import telemetry
from agent import run_langchain_agent
from workflow import LangChainAgentWorkflow


async def connect_with_retry(address: str, namespace: str) -> Client:
    """Retry the initial connection, matching dialTemporal in the Go worker.

    Compose reports the Temporal container healthy before the Frontend Service
    will accept connections; crash-looping on that produces a confusing first
    run for no reason.
    """
    last: Exception | None = None
    for attempt in range(1, 31):
        try:
            client = await Client.connect(address, namespace=namespace)
            logging.info("connected to Temporal at %s (namespace %s)", address, namespace)
            return client
        except Exception as exc:  # noqa: BLE001 — retried below
            last = exc
            logging.info("Temporal not ready (attempt %d/30): %s", attempt, exc)
            await asyncio.sleep(2)
    raise RuntimeError(f"unable to connect to Temporal: {last}")


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="[agent-langchain] %(asctime)s %(levelname)s %(message)s",
    )

    telemetry.init_metrics("temporal-agent-langchain")

    client = await connect_with_retry(
        os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        os.getenv("TEMPORAL_NAMESPACE", "default"),
    )

    task_queue = os.getenv("AGENT_TASK_QUEUE", "agents-langchain")

    # The concurrency number that matters for an agent fleet.
    #
    # The Go order Worker defaults to 10 activity slots because its Activities
    # are CPU-bound and finish in milliseconds. An agent Activity spends almost
    # all of its wall-clock time blocked on a provider's HTTP response, so slots
    # sized for CPU work leave the Worker idle while its queue backs up — and
    # the symptom is rising schedule-to-start with flat CPU, which reads exactly
    # like "we need more Workers" when the real answer is "raise this number".
    #
    # That distinction is the whole subject of docs/SCALE-TESTING.md, and the
    # agent fleet is where it bites hardest.
    max_activities = int(os.getenv("MAX_CONCURRENT_ACTIVITIES", "50"))

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[LangChainAgentWorkflow],
        activities=[run_langchain_agent],
        max_concurrent_activities=max_activities,
    )

    logging.info(
        "worker polling task queue %r (activity slots=%d)", task_queue, max_activities
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
