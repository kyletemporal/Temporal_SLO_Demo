import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

export const API_BASE = __ENV.API_BASE || 'http://localhost:8081';

// Tracked separately from k6's built-in http_req_failed so the console summary
// distinguishes "the API rejected us" from "the API was slow". A 503 here
// usually means the Temporal Frontend pushed back, which is itself a finding.
export const startRejected = new Counter('order_start_rejected');
export const startAccepted = new Counter('order_start_accepted');

/**
 * Starts one OrderWorkflow.
 *
 * Fire-and-forget by default (wait: false). This matters: if k6 blocked on
 * workflow completion, the load generator would self-throttle the moment
 * Workers fell behind, and the backlog scenarios could never build a backlog.
 */
export function startOrder(payload) {
  const res = http.post(`${API_BASE}/orders`, JSON.stringify(payload), {
    headers: { 'Content-Type': 'application/json' },
    tags: { name: 'POST /orders' },
    timeout: '30s',
  });

  const ok = check(res, {
    'workflow accepted': (r) => r.status === 202 || r.status === 200,
  });

  if (ok) {
    startAccepted.add(1);
  } else {
    startRejected.add(1);
  }

  return res;
}

export function orderId(prefix) {
  return `${prefix}-${__VU}-${__ITER}-${Date.now()}`;
}

export const agentRejected = new Counter('agent_start_rejected');
export const agentAccepted = new Counter('agent_start_accepted');

/**
 * Starts one AgentWorkflow.
 *
 * Fire-and-forget for the same reason startOrder is: an agent run takes
 * seconds, so blocking on completion would make the load generator
 * self-throttle to the agent's own latency and no backlog could ever form.
 *
 * Defaults to the offline mock provider. Pointing this at a real provider from
 * a load test is a way to spend a lot of money quickly — set provider
 * explicitly and deliberately, and read the warning in docs/SCALE-TESTING.md
 * first.
 */
export function startAgent(payload) {
  const res = http.post(`${API_BASE}/agent`, JSON.stringify(payload), {
    headers: { 'Content-Type': 'application/json' },
    tags: { name: 'POST /agent' },
    timeout: '30s',
  });

  const ok = check(res, {
    'agent accepted': (r) => r.status === 202 || r.status === 200,
  });

  if (ok) {
    agentAccepted.add(1);
  } else {
    agentRejected.add(1);
  }

  return res;
}

export function banner(title, watch) {
  console.log('');
  console.log('='.repeat(72));
  console.log(`  SCENARIO: ${title}`);
  console.log('='.repeat(72));
  console.log('  Watch these panels in Grafana (Temporal Self-Hosted Overview):');
  watch.forEach((w) => console.log(`    - ${w}`));
  console.log('='.repeat(72));
  console.log('');
}
