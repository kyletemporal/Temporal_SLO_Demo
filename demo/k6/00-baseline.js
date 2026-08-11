import { startOrder, orderId, banner } from './lib/api.js';

/**
 * SCENARIO 0 — Baseline
 *
 * Healthy steady-state traffic. Run this FIRST and let it settle for at least
 * ten minutes before touching anything else.
 *
 * This is not a throwaway step. Every threshold in temporal-alerts.yml is a
 * guess until you have seen what normal looks like on your own hardware, and
 * an alert tuned against no baseline is an alert the team will learn to
 * ignore inside a week.
 *
 * Record from this run:
 *   - Frontend P95 latency per operation
 *   - Persistence P95 latency per operation
 *   - Sync match rate (should sit at or near 100%)
 *   - Schedule-to-start P99 (should sit near zero)
 */
export const options = {
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate',
      rate: 20,
      timeUnit: '1s',
      duration: __ENV.DURATION || '10m',
      preAllocatedVUs: 20,
      maxVUs: 100,
    },
  },
  thresholds: {
    // If the API cannot accept a modest 20/s, something is wrong with the
    // stack itself and the later scenarios will be uninterpretable.
    // Plain 'checks', not 'checks{}'. k6 0.55 parses an empty tag expression
    // as an empty metric name and refuses to start the run at all.
    checks: ['rate>0.99'],
    'http_req_duration{name:POST /orders}': ['p(95)<500'],
  },
};

export function setup() {
  banner('Baseline — healthy steady state', [
    'Frontend Availability   → expect ~100%',
    'Sync Match Rate         → expect >99%',
    'Schedule-to-Start P99   → expect near zero',
    'Worker Task Slots       → expect well above zero',
    'Workflow Outcomes       → expect success only',
  ]);
}

export default function () {
  startOrder({
    orderId: orderId('base'),
    failureRate: 0,
    activityDelayMs: 50,
    maxAttempts: 3,
  });
}
