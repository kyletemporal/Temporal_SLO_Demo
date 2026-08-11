import { startOrder, orderId, banner } from './lib/api.js';

/**
 * SCENARIO 4 — Slot saturation at low load
 *
 * Uses very slow Activities at a deliberately gentle arrival rate. Total
 * throughput here is trivial — a handful of orders per second — yet the Worker
 * still runs out of execution slots because each Activity occupies one for
 * thirty seconds.
 *
 * WHAT YOU ARE DEMONSTRATING
 * That "add more Workers" is often the wrong reflex.
 *
 * This produces the same dashboard shape as Scenario 1 — schedule-to-start
 * climbing, slots at zero — but the underlying cause is completely different.
 * Here the Worker host is nearly idle. CPU is low, memory is low, and the
 * constraint is a configuration value (MAX_CONCURRENT_ACTIVITIES, set to 10)
 * rather than hardware.
 *
 * The discriminator is host CPU, which is NOT a Temporal metric:
 *
 *   slots at zero + host CPU HIGH → real capacity shortage, add Workers
 *   slots at zero + host CPU LOW  → concurrency limit too low, raise it
 *
 * This is exactly why the guide insists on having node-level metrics in the
 * same Grafana. Without them this scenario and Scenario 1 are indistinguishable
 * on the dashboard, and half the runbook's decision branches are unusable.
 *
 * TRY IT BOTH WAYS:
 *   1. Run as-is. Slots hit zero, latency climbs, `docker stats` shows an idle
 *      Worker container.
 *   2. Raise the limit and rerun:
 *        MAX_CONCURRENT_ACTIVITIES=200 docker compose up -d worker
 *      Same load, same hardware, problem gone. No Workers were added.
 */
export const options = {
  scenarios: {
    saturate: {
      executor: 'constant-arrival-rate',
      rate: 2,
      timeUnit: '1s',
      duration: __ENV.DURATION || '6m',
      preAllocatedVUs: 10,
      maxVUs: 50,
    },
  },
};

export function setup() {
  banner('Slot saturation — starved Worker on idle hardware', [
    'Worker Task Slots       → floors at zero',
    'Schedule-to-Start P99   → climbs steadily',
    'Frontend Request Rate   → stays LOW. This is the tell.',
    'RUN ALONGSIDE: `docker stats` — the Worker container is barely working',
    'FIX WITHOUT SCALING: MAX_CONCURRENT_ACTIVITIES=200 docker compose up -d worker',
  ]);
}

export default function () {
  startOrder({
    orderId: orderId('slot'),
    failureRate: 0,
    // 30s per Activity. With 10 slots, one Worker sustains roughly 0.33
    // Activities/sec. At 2 orders/sec (3 Activities each) demand is about
    // 6/sec — an 18x overcommit at almost no CPU cost.
    activityDelayMs: 30000,
    maxAttempts: 1,
  });
}
