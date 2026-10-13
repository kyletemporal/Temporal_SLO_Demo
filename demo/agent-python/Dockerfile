# syntax=docker/dockerfile:1
#
# The LangChain worker is an OPTIONAL service. It is behind the `langchain`
# compose profile, so `make up` does not build it and the default demo needs
# neither this image nor an API key.
#
# That matters because this image is large — LangChain's dependency tree is
# several hundred megabytes — and paying that on every `make up` to run a path
# most people will not exercise is a bad trade. `make agent-langchain` opts in.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements first so the dependency layer caches independently of the source.
# Without this split every edit to agent.py re-resolves the whole LangChain tree.
COPY requirements.txt .
RUN pip install -r requirements.txt

# test_parity.py rides along deliberately. It is the only way to run the
# contract check on a host without the OTel Python packages installed — which
# is most hosts, since this image exists precisely so nobody has to install
# them. A few KB of pure-Python test in exchange for a check that can actually
# be run is the right trade.
COPY telemetry.py agent.py workflow.py worker.py test_parity.py ./

# Non-root. SECURITY.md finding P4 applies to every service in this stack, and
# a new one should not arrive already in violation.
RUN useradd --create-home --uid 10001 agent && chown -R agent:agent /app
USER agent

CMD ["python", "worker.py"]
