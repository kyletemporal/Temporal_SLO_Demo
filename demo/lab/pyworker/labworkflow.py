"""The lab Workflow, Python edition. Same name, same shape and same input as the
Go one in ../shared.go, so the two lanes run identical work and differ only in
the SDK executing it.

Kept in its own module because the Workflow sandbox re-imports the module a
Workflow is defined in; nothing here may do I/O or read the clock.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn(name="LabWorkflow")
class LabWorkflow:
    @workflow.run
    async def run(self, inp: dict) -> int:
        done = 0
        for phase in range(inp["phases"]):
            handles = [
                workflow.execute_activity(
                    "LabActivity",
                    {"lane": inp["lane"], "phase": phase, "index": i},
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1), maximum_attempts=3
                    ),
                )
                for i in range(inp["activitiesPerPhase"])
            ]
            for h in handles:
                await h
                done += 1
        return done
