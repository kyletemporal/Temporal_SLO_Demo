"""Python Worker for the tuning lab.

Polls the controller for its lane's config, runs a real Temporal Worker with
those options, and reports the SDK's own metrics back every 500ms in the same
shape as the Go Worker (see ../telemetry.go, WorkerReport).

Differences from the Go Worker that the UI surfaces rather than hides:

- Every option, sticky cache included, is per Worker in the Python SDK, so all
  changes apply by swapping the Worker in place — no process restart.
- CPU-bound Activities run in a thread pool and contend for the GIL. Adding
  slots adds threads, not cores; the "cpus" knob does not apply to Python.
- Metrics come from the SDK's Rust core through MetricBuffer, so names and
  units follow core, not the Python layer.
"""

import asyncio
import concurrent.futures
import hashlib
import json
import os
import random
import resource
import socket
import time
import urllib.request
from bisect import bisect_left
from datetime import datetime, timedelta, timezone

from temporalio import activity
from temporalio.client import Client
from temporalio.runtime import (
    MetricBuffer,
    MetricBufferDurationFormat,
    Runtime,
    TelemetryConfig,
)
from temporalio.worker import Worker

from labworkflow import LabWorkflow

LANE = os.environ.get("LANE", "python")
CONTROLLER = os.environ.get("CONTROLLER_URL", "http://lab-controller:8095")
TEMPORAL = os.environ.get("TEMPORAL_ADDRESS", "temporal:7233")
NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
WORKER_ID = f"{LANE}-{socket.gethostname()}"
STARTED_AT = datetime.now(timezone.utc).isoformat()
WINDOW = 10.0  # seconds, matches latencyWindow in telemetry.go

# Core metric name (prefix stripped) -> canonical key. Mirrors telemetry.go.
TIMERS = {
    "workflow_task_schedule_to_start_latency": "wft_s2s",
    "workflow_task_execution_latency": "wft_exec",
    "activity_schedule_to_start_latency": "act_s2s",
    "activity_execution_latency": "act_exec",
    "workflow_task_replay_latency": "replay",
    "workflow_endtoend_latency": "wf_e2e",
}
TIMER_COUNTS = {"wft_exec": "wft_completed", "act_exec": "act_completed"}
COUNTERS = {
    "workflow_completed": "wf_completed",
    "activity_execution_failed": "act_failed",
    "sticky_cache_hit": "cache_hit",
    "sticky_cache_miss": "cache_miss",
}
WORKER_KIND = {"WorkflowWorker": "wft", "ActivityWorker": "act"}
POLLER_KIND = {
    "workflow_task": "pollers_workflow",
    "sticky_workflow_task": "pollers_sticky",
    "workflow_sticky_task": "pollers_sticky",
    "activity_task": "pollers_activity",
}


def gauge_key(name: str, attrs: dict) -> str | None:
    if name in ("worker_task_slots_used", "worker_task_slots_available"):
        kind = WORKER_KIND.get(str(attrs.get("worker_type", "")))
        return f"{kind}_slots_{name.removeprefix('worker_task_slots_')}" if kind else None
    if name == "num_pollers":
        return POLLER_KIND.get(str(attrs.get("poller_type", "")))
    if name == "sticky_cache_size":
        return "cache_size"
    return None


class Telemetry:
    def __init__(self, buffer: MetricBuffer) -> None:
        self.buffer = buffer
        self.counters: dict[str, float] = {}
        self.gauges: dict[str, float] = {}
        self.samples: dict[str, list[tuple[float, float]]] = {}
        self.seen_names: set[str] = set()

    def drain(self) -> None:
        now = time.monotonic()
        for u in self.buffer.retrieve_updates():
            name = u.metric.name.removeprefix("temporal_")
            self.seen_names.add(name)
            if name == "long_request_latency":
                # See pollKey in ../telemetry.go: poll latency separates
                # poller-bound from server-bound.
                op = str(dict(u.attributes).get("operation", ""))
                key = ("poll_act" if "PollActivityTaskQueue" in op
                       else "poll_wft" if "PollWorkflowTaskQueue" in op else None)
                if key:
                    self.samples.setdefault(key, []).append((now, float(u.value)))
            elif name in TIMERS:
                key = TIMERS[name]
                self.samples.setdefault(key, []).append((now, float(u.value)))
                if key in TIMER_COUNTS:
                    c = TIMER_COUNTS[key]
                    self.counters[c] = self.counters.get(c, 0) + 1
            elif name in COUNTERS:
                c = COUNTERS[name]
                self.counters[c] = self.counters.get(c, 0) + float(u.value)
            else:
                key = gauge_key(name, dict(u.attributes))
                if key:
                    self.gauges[key] = float(u.value)

    def reset_gauges(self) -> None:
        # After a swap, only the new Worker's gauges should count.
        self.gauges.clear()

    def latency(self) -> dict:
        cutoff = time.monotonic() - WINDOW
        out = {}
        for key, s in self.samples.items():
            i = bisect_left(s, (cutoff, float("-inf")))
            s = self.samples[key] = s[i:][-20000:]
            vals = sorted(v for _, v in s)
            if not vals:
                out[key] = {"p50": 0, "p95": 0, "mean": 0, "n": 0}
                continue
            out[key] = {
                "p50": vals[int(0.50 * (len(vals) - 1))],
                "p95": vals[int(0.95 * (len(vals) - 1))],
                "mean": sum(vals) / len(vals),
                "n": len(vals),
            }
        return out


def cpu_seconds() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def cpu_limit() -> float:
    """Cores available to the Activity and Workflow code — which, for Python,
    is ONE: the GIL lets one thread run Python bytecode at a time, however many
    cores the container has. Reporting the container's 2 cores made a GIL-bound
    Worker read as "CPU 51%, idle" and the diagnosis call it slot-bound.
    (Measured CPU can exceed 1.0 slightly: the SDK's Rust core runs its own
    threads outside the GIL.)"""
    return min(1.0, container_cpus())


def container_cpus() -> float:
    # cgroup v2 quota if the container has one, else the visible CPUs.
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
        if quota != "max":
            return int(quota) / int(period)
    except (OSError, ValueError):
        pass
    return float(os.cpu_count() or 1)


def rss_mb() -> float:
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def calibrate() -> float:
    rounds = 100_000
    start = time.perf_counter()
    burn(rounds)
    return rounds / max((time.perf_counter() - start) * 1000, 1e-3)


def burn(rounds: int) -> None:
    # Small inputs: hashlib keeps the GIL, as most real CPU-bound Python does.
    b = b"temporal-tuning-lab"
    for _ in range(rounds):
        b = hashlib.sha256(b).digest()


def http_json(method: str, url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


async def downstream(ms: int) -> None:
    """Real HTTP to the controller's fake dependency, on the event loop, so a
    queued call costs a slot and no thread."""
    host, port = CONTROLLER.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(f"GET /downstream?lane={LANE}&ms={ms} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
    await writer.drain()
    status = await reader.readline()
    writer.close()
    if b" 200 " not in status:
        raise RuntimeError(f"downstream returned {status!r}")


class LabWorker:
    def __init__(self, runtime: Runtime, telemetry: Telemetry) -> None:
        self.runtime = runtime
        self.telemetry = telemetry
        self.cfg: dict | None = None
        self.version = 0
        self.worker: Worker | None = None
        self.worker_task: asyncio.Task | None = None
        self.cpu_pool: concurrent.futures.ThreadPoolExecutor | None = None
        self.rounds_per_ms = calibrate()
        self.note = (f"Python: CPU limit shown is the GIL's 1 core (container has {container_cpus():g}); "
                     "the cpus knob does not apply.")
        print(f"calibrated CPU work: {self.rounds_per_ms:.0f} sha256 rounds per ms", flush=True)

    @activity.defn(name="LabActivity")
    async def lab_activity(self, inp: dict) -> None:
        c = self.cfg
        d = c["durationMs"] / 1000
        if c["jitterPct"]:
            d += d * c["jitterPct"] / 100 * (random.random() * 2 - 1)
        if c["profile"] == "cpu":
            rounds = int(self.rounds_per_ms * d * 1000)
            await asyncio.get_running_loop().run_in_executor(self.cpu_pool, burn, rounds)
        elif c["profile"] == "io":
            await downstream(int(d * 1000))
        else:
            await asyncio.sleep(d)

    async def swap(self, c: dict) -> None:
        """Start the new Worker before draining the old one, so the queue never
        has zero pollers.

        Each Worker gets its OWN client connection. The Python SDK (1.34)
        refuses a second Worker for the same Task Queue on one client —
        "Registration of multiple workers with overlapping worker task types"
        — which made the first version of this swap fail on every change while
        the old Worker kept running with the old options. Go has the same shape
        of fix: client.NewClientFromExisting per Worker.
        """
        client = await Client.connect(TEMPORAL, namespace=NAMESPACE, runtime=self.runtime)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(c["actSlots"], 1))
        worker = Worker(
            client,
            task_queue=f"lab-{LANE}",
            workflows=[LabWorkflow],
            activities=[self.lab_activity],
            activity_executor=pool,
            max_concurrent_workflow_task_polls=max(c["wfPollers"], 2),
            max_concurrent_workflow_tasks=max(c["wfSlots"], 2),
            max_concurrent_activity_task_polls=max(c["actPollers"], 1),
            max_concurrent_activities=max(c["actSlots"], 1),
            max_cached_workflows=max(c["cacheSize"], 1),
            graceful_shutdown_timeout=timedelta(seconds=30),
        )
        old_worker, old_task, old_pool = self.worker, self.worker_task, self.cpu_pool
        self.worker, self.cpu_pool = worker, pool
        self.worker_task = asyncio.create_task(worker.run())
        self.telemetry.reset_gauges()
        if old_worker is not None:
            async def drain():
                await old_worker.shutdown()
                await asyncio.gather(old_task, return_exceptions=True)
                old_pool.shutdown(wait=False)
                print("drained previous Worker", flush=True)
            asyncio.create_task(drain())

    async def apply(self, msg: dict) -> None:
        c = msg["config"]
        keys = ("wfPollers", "wfSlots", "actPollers", "actSlots", "cacheSize")
        if self.cfg is None or any(self.cfg[k] != c[k] for k in keys):
            # Swap first. Only a successful swap may record the config as
            # applied; on failure the next poll retries the same version.
            await self.swap(c)
        self.cfg = c
        self.version = msg["version"]
        print(f"config v{self.version} applied: {c}", flush=True)

    async def config_loop(self) -> None:
        while True:
            try:
                msg = await asyncio.to_thread(http_json, "GET", f"{CONTROLLER}/api/config?lane={LANE}")
                if msg["version"] != self.version:
                    await self.apply(msg)
            except Exception as e:  # controller restarting: keep the current Worker
                print(f"config poll failed: {e}", flush=True)
            await asyncio.sleep(1)

    async def report_loop(self) -> None:
        last_cpu, last_at = cpu_seconds(), time.monotonic()
        logged = False
        while True:
            await asyncio.sleep(0.5)
            self.telemetry.drain()
            now, cpu = time.monotonic(), cpu_seconds()
            cores = (cpu - last_cpu) / (now - last_at)
            last_cpu, last_at = cpu, now
            if not logged and self.telemetry.seen_names:
                print("core metric names seen:", sorted(self.telemetry.seen_names), flush=True)
                logged = True
            report = {
                "lane": LANE, "workerId": WORKER_ID, "sdk": "python 1.34.0",
                "configVersion": self.version, "startedAt": STARTED_AT,
                "counters": self.telemetry.counters, "gauges": self.telemetry.gauges,
                "latency": self.telemetry.latency(),
                "cpuCores": cores, "cpuLimit": cpu_limit(), "rssMB": rss_mb(),
                "note": self.note,
            }
            try:
                await asyncio.to_thread(http_json, "POST", f"{CONTROLLER}/api/report", report)
            except Exception:
                pass


async def main() -> None:
    buffer = MetricBuffer(100_000, duration_format=MetricBufferDurationFormat.SECONDS)
    runtime = Runtime(telemetry=TelemetryConfig(metrics=buffer))
    lab = LabWorker(runtime, Telemetry(buffer))

    while lab.cfg is None:
        try:
            await lab.apply(await asyncio.to_thread(http_json, "GET", f"{CONTROLLER}/api/config?lane={LANE}"))
        except Exception as e:
            print(f"waiting for controller: {e}", flush=True)
            await asyncio.sleep(2)

    await asyncio.gather(lab.config_loop(), lab.report_loop())


if __name__ == "__main__":
    asyncio.run(main())
