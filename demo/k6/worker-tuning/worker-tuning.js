/**
 * Worker tuning load test: starts Workflows at stepped, fixed rates straight
 * against the Temporal frontend over gRPC. There is no application in the
 * path, so it runs unchanged against Temporal Cloud, a self-hosted cluster or
 * a dev server.
 *
 * WHAT IT MEASURES, AND WHAT IT DOES NOT
 *   k6 measures whether the SERVER ACCEPTED the starts, and how fast. It does
 *   not measure your Workers. Whether the Workers kept up is in their own SDK
 *   metrics (schedule-to-start, slots) and in the teardown count of Workflows
 *   still running. A clean k6 summary with a growing Running count means the
 *   Workers fell behind, which is the finding a tuning test exists to produce.
 *
 * HOW TO USE IT TO TUNE
 *   1. Run it once with your current Worker config (or TUNING_MODE=SDK_DEFAULTS).
 *   2. Change ONE tuning value (flip the flag), wait for the swap, run it again.
 *   3. Compare the step where schedule-to-start first rises. That step is your
 *      capacity, and the tuning change either moved it or did not.
 *
 * Quick start (local dev server, the lab's Java Worker on lab-java):
 *   k6 run worker-tuning.js
 * Temporal Cloud, API key:
 *   TEMPORAL_ADDRESS=<region>.<cloud>.api.temporal.io:7233 TEMPORAL_NAMESPACE=<ns>.<acct> \
 *   TEMPORAL_API_KEY=... TASK_QUEUE=<your queue> WORKFLOW_TYPE=<your type> \
 *   INPUT_JSON='{"...":"..."}' k6 run worker-tuning.js
 * Temporal Cloud, mTLS:
 *   TEMPORAL_ADDRESS=<ns>.<acct>.tmprl.cloud:7233 TEMPORAL_TLS_CERT=client.pem TEMPORAL_TLS_KEY=client.key ...
 *
 * See README.md for every variable and the landmines.
 */
import grpc from 'k6/net/grpc';
import encoding from 'k6/encoding';
import exec from 'k6/execution';
import { sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { uuidv4 } from './uuid.js';

const E = (k, d) => (__ENV[k] === undefined || __ENV[k] === '' ? d : __ENV[k]);

const ADDRESS = E('TEMPORAL_ADDRESS', 'localhost:7233');
const NAMESPACE = E('TEMPORAL_NAMESPACE', 'lab');
const TASK_QUEUE = E('TASK_QUEUE', 'lab-java');
const WORKFLOW_TYPE = E('WORKFLOW_TYPE', 'LabWorkflow');
const INPUT_JSON = E('INPUT_JSON', '{"lane":"java","phases":3,"activitiesPerPhase":2}');
const API_KEY = E('TEMPORAL_API_KEY', '');
const TLS_CERT = E('TEMPORAL_TLS_CERT', '');
const TLS_KEY = E('TEMPORAL_TLS_KEY', '');
const TLS_CA = E('TEMPORAL_TLS_CA', '');
// TLS defaults ON for anything that has credentials. Cloud never accepts plaintext.
const TLS = E('TEMPORAL_TLS', API_KEY || TLS_CERT ? 'true' : 'false') === 'true';
// Leftovers from an aborted run end on their own instead of piling up as
// "stuck" Workflows on your SLO dashboards. Set it above your Workflow's real
// duration plus the worst backlog you expect, or you terminate real work.
const EXEC_TIMEOUT = E('WORKFLOW_EXECUTION_TIMEOUT', '15m');
const ID_PREFIX = E('WORKFLOW_ID_PREFIX', 'k6-tuning');
// protobuf JSON wants a Duration as seconds ("900s"); accept 30s / 15m / 2h.
const EXEC_TIMEOUT_PROTO = (() => {
  const m = /^(\d+)([smh])$/.exec(EXEC_TIMEOUT);
  if (!m) throw new Error(`WORKFLOW_EXECUTION_TIMEOUT must look like 30s, 15m or 2h, got ${EXEC_TIMEOUT}`);
  return `${Number(m[1]) * { s: 1, m: 60, h: 3600 }[m[2]]}s`;
})();

// Stepped, OPEN-model load: "rate,hold" pairs in starts/s and seconds, with a
// RAMP between steps. Plateaus, not a continuous ramp: each step must hold long
// enough for schedule-to-start to settle (several Workflow durations plus a
// couple of scrape intervals), or you are reading the transient, not capacity.
const STEPS = E('STEPS', '2,120 4,120 8,120 16,120')
  .trim()
  .split(/\s+/)
  .map((s) => s.split(',').map(Number));
const RAMP = Number(E('RAMP_S', '20'));

// Pre-encoded once: the payload is identical for every start.
const PAYLOAD = {
  payloads: [
    {
      metadata: { encoding: encoding.b64encode('json/plain') },
      data: encoding.b64encode(INPUT_JSON),
    },
  ],
};

// One id per run, so the teardown count and any cleanup query only ever touch
// THIS run's Workflows, never a previous run's or a real one. Generated in
// setup() and passed to every VU as `data`: init code runs once PER VU, and the
// first version computed it there. Measured: 4 VUs stamped 4 different ids
// (mw11vjzk..n), none matched setup's, so teardown counted 0 of 88 still running.
const RUN_ID = E('RUN_ID', '');

const startLatency = new Trend('temporal_start_latency', true);
const startsOk = new Counter('temporal_starts_ok');
const startsFailed = new Counter('temporal_starts_failed');
const startsThrottled = new Counter('temporal_starts_throttled');

const stages = [];
for (const [rate, hold] of STEPS) {
  stages.push({ target: rate, duration: `${RAMP}s` }, { target: rate, duration: `${hold}s` });
}
const peak = Math.max(...STEPS.map((s) => s[0]));

export const options = {
  scenarios: {
    steps: {
      // OPEN model on purpose. A closed loop (VUs that wait) slows down to
      // match whatever the server and Workers manage, so it can never build the
      // backlog a capacity test is looking for.
      executor: 'ramping-arrival-rate',
      startRate: STEPS[0][0],
      timeUnit: '1s',
      stages,
      preAllocatedVUs: Math.max(10, peak * 2),
      maxVUs: Math.max(50, peak * 10),
    },
  },
  thresholds: {
    // Failing these means the TEST is invalid, not that the Workers are slow:
    // load that was never delivered cannot tell you anything about capacity.
    temporal_starts_failed: [{ threshold: 'count<1', abortOnFail: false }],
    temporal_starts_throttled: [{ threshold: 'count<1', abortOnFail: false }],
    // k6 itself could not keep the rate (not enough VUs, or a slow start RPC).
    dropped_iterations: [{ threshold: 'count<1', abortOnFail: false }],
    temporal_start_latency: ['p(99)<1000'],
  },
  // Thresholds and summary per step, by target rate.
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

const client = new grpc.Client();
client.loadProtoset('./temporal-api.protoset');

let tlsParams;
if (TLS_CERT) {
  tlsParams = { cert: open(TLS_CERT), key: open(TLS_KEY) };
  if (TLS_CA) tlsParams.cacerts = [open(TLS_CA)];
}

function metadata() {
  const md = {};
  if (API_KEY) {
    md.authorization = `Bearer ${API_KEY}`;
    // Required with API keys: Cloud routes an API-key request by this header.
    // Without it a namespace endpoint can reject the call as unauthenticated
    // although the key is valid.
    md['temporal-namespace'] = NAMESPACE;
  }
  return md;
}

function connect() {
  const p = { plaintext: !TLS, timeout: '10s' };
  if (tlsParams) p.tls = tlsParams;
  client.connect(ADDRESS, p);
}

let connected = false;

/** Which step (by target rate) this moment of the test belongs to. */
function currentStep() {
  let t = exec.instance.currentTestRunDuration / 1000;
  for (const [rate, hold] of STEPS) {
    t -= RAMP + hold;
    if (t <= 0) return String(rate);
  }
  return String(STEPS[STEPS.length - 1][0]);
}

export default function (data) {
  if (!connected) {
    connect();
    connected = true;
  }
  const step = currentStep();
  const tags = { step };
  const req = {
    namespace: NAMESPACE,
    // VU + iteration make it unique within the run; RUN_ID across runs. A
    // duplicate id is rejected (or deduplicated) by the server and would be
    // silently counted as load that never ran.
    workflowId: `${ID_PREFIX}-${data.runId}-${exec.vu.idInTest}-${exec.vu.iterationInScenario}`,
    workflowType: { name: WORKFLOW_TYPE },
    taskQueue: { name: TASK_QUEUE, kind: 'TASK_QUEUE_KIND_NORMAL' },
    input: PAYLOAD,
    workflowExecutionTimeout: EXEC_TIMEOUT_PROTO,
    identity: `k6-worker-tuning@${exec.vu.idInTest}`,
    // Must be unique per start. The server deduplicates retried starts by it.
    requestId: uuidv4(),
  };
  const t0 = Date.now();
  const res = client.invoke('temporal.api.workflowservice.v1.WorkflowService/StartWorkflowExecution', req, {
    metadata: metadata(),
    timeout: '10s',
    tags,
  });
  startLatency.add(Date.now() - t0, tags);
  if (res && res.status === grpc.StatusOK) {
    startsOk.add(1, tags);
  } else if (res && res.status === grpc.StatusResourceExhausted) {
    // Namespace RPS / APS limit (Cloud) or frontend rate limit (self-hosted).
    // The load you asked for did not reach the Workers. Fix the limit before
    // reading anything about Worker capacity from this step.
    startsThrottled.add(1, tags);
  } else {
    startsFailed.add(1, { ...tags, code: String(res ? res.status : 'none') });
    if (exec.vu.iterationInScenario < 3) {
      console.error(`start failed: status=${res && res.status} ${res && JSON.stringify(res.error)}`);
    }
  }
}

export function setup() {
  const runId = RUN_ID || Date.now().toString(36);
  console.log(
    `worker-tuning: ${ADDRESS} ns=${NAMESPACE} tq=${TASK_QUEUE} type=${WORKFLOW_TYPE} ` +
      `tls=${TLS} auth=${API_KEY ? 'api-key' : TLS_CERT ? 'mtls' : 'none'} run=${runId} ` +
      `steps=${STEPS.map((s) => `${s[0]}/s×${s[1]}s`).join(' → ')}`,
  );
  console.log(`cleanup if needed: temporal workflow terminate --namespace ${NAMESPACE} ` +
    `--query 'WorkflowId STARTS_WITH "${ID_PREFIX}-${runId}-" AND ExecutionStatus="Running"' --reason "k6 cleanup"`);
  return { runId };
}

/**
 * How many of THIS run's Workflows are still open when the load stops. This is
 * the one Worker-side number k6 can see: a non-zero count is backlog the
 * Workers never caught up on. Visibility lags writes by up to a few seconds,
 * hence the wait. It needs advanced visibility (SQL on 1.20+, Elasticsearch, or
 * Cloud) for STARTS_WITH; on standard visibility the count call fails and is
 * reported, not hidden.
 */
export function teardown(data) {
  connect();
  const wait = Number(E('TEARDOWN_WAIT_S', '10'));
  sleep(wait);
  const res = client.invoke(
    'temporal.api.workflowservice.v1.WorkflowService/CountWorkflowExecutions',
    {
      namespace: NAMESPACE,
      query: `WorkflowId STARTS_WITH "${ID_PREFIX}-${data.runId}-" AND ExecutionStatus="Running"`,
    },
    { metadata: metadata(), timeout: '15s' },
  );
  if (res.status === grpc.StatusOK) {
    console.log(`teardown: ${res.message.count || 0} Workflows from this run still Running ${wait}s after load stopped`);
  } else {
    console.warn(`teardown: could not count running Workflows (status ${res.status}): ${JSON.stringify(res.error)}`);
  }
  client.close();
}
