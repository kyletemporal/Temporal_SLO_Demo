"""Temporal Workflow wrapping the LangChain agent.

Thin on purpose. The interesting content is in agent.py, and the contrast with
demo/app/agent_workflow.go is the reason this file is only twenty lines of logic
where the Go one is a hundred: LangChain owns the loop, so there is no loop here
to own.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

# Activity types are imported through the sandbox's pass-through so that the
# Workflow can reference them without importing LangChain — which would fail
# sandbox validation for module-level non-determinism.
with workflow.unsafe.imports_passed_through():
    from agent import AgentReply, AgentRequest, run_langchain_agent


@workflow.defn
class LangChainAgentWorkflow:
    @workflow.run
    async def run(self, req: AgentRequest) -> AgentReply:
        return await workflow.execute_activity(
            run_langchain_agent,
            req,
            # Generous: this single Activity contains the ENTIRE agent loop, so
            # its timeout has to cover every model call and every tool call put
            # together — not one model call, as in the Go worker.
            start_to_close_timeout=timedelta(minutes=15),
            # ONE ATTEMPT, DELIBERATELY.
            #
            # This Activity is not resumable. A failure on the ninth model call
            # discards the eight already paid for, and a retry starts again at
            # call one — so the default three attempts can cost three times the
            # full loop to recover from one transient 529.
            #
            # The Go agent retries happily because its Activity is a SINGLE
            # model call: retrying re-pays for one call, not for the run.
            # Whether to spend the money twice here is a business decision, so
            # it is made explicitly rather than inherited from a default.
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
