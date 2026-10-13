"""End-to-end smoke test for a freshly deployed Temporal cluster.

Runs inside the cluster as a Job (scripts/smoke-test.sh). It proves the path a
real application takes, not just that pods are Ready:

  1. frontend accepts a connection and a StartWorkflowExecution
  2. history persists it, matching dispatches the Workflow Task to THIS worker
  3. an Activity is scheduled, dispatched, completed and recorded
  4. the closed execution appears in VISIBILITY (temporal_visibility on
     Postgres), which is a separate write path from history and the one that
     most often lags or breaks independently

Exits non-zero on the first thing that does not happen.
"""

import asyncio
import os
import sys
import time
import uuid
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc:7233")
NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = f"smoke-{uuid.uuid4().hex[:8]}"


@activity.defn
async def greet(name: str) -> str:
    return f"hello, {name}"


@workflow.defn
class SmokeWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            greet, name, start_to_close_timeout=timedelta(seconds=30)
        )


def step(msg: str) -> None:
    print(f"==> {msg}", flush=True)


async def main() -> int:
    step(f"connecting to {ADDRESS} namespace={NAMESPACE}")
    client = await Client.connect(ADDRESS, namespace=NAMESPACE)

    workflow_id = f"smoke-{uuid.uuid4()}"

    # Unsandboxed only because this file is __main__; the sandbox re-imports
    # the workflow's module, which a script run as main cannot support.
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[SmokeWorkflow],
        activities=[greet],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        step(f"executing workflow {workflow_id} on task queue {TASK_QUEUE}")
        started = time.monotonic()
        result = await client.execute_workflow(
            SmokeWorkflow.run,
            "temporal",
            id=workflow_id,
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(minutes=2),
        )
        elapsed = time.monotonic() - started
        if result != "hello, temporal":
            print(f"FAIL unexpected result {result!r}", flush=True)
            return 1
        step(f"workflow completed in {elapsed:.2f}s with {result!r}")

    # Visibility is eventually consistent; give it a bounded window.
    step("waiting for the closed execution to appear in visibility")
    query = f'WorkflowId = "{workflow_id}" AND ExecutionStatus = "Completed"'
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        found = [w async for w in client.list_workflows(query)]
        if found:
            step(f"visibility shows {found[0].id} as {found[0].status.name}")
            print("PASS", flush=True)
            return 0
        await asyncio.sleep(2)

    print(f"FAIL workflow completed but is not in visibility after 60s: {query}", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
