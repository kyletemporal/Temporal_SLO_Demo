import { startOrder, orderId, banner } from './lib/api.js';

/**
 * SCENARIO 1 — Backlog storm
 *
 * Ramps arrival rate far past what the Worker fleet can drain, using slow
 * Activities so each in-flight Task holds a slot.
 *
 * This is the single most common real Temporal incident: work arrives faster
 * than Workers consume it, and Tasks sit in the queue.
 *
 * WHAT YOU ARE DEMONSTRATING
 * The diagnostic fork in the runbook. Schedule-to-start latency alone tells
 * you there is a backlog but not why. Sync match rate splits the two causes
 * apart:
 *
 *   high schedule-to-start + HIGH sync match → not enough Worker capacity
 *   high schedule-to-start + LOW  sync match → Tasks queuing before delivery
 *
 * This scenario produces the second shape. Scale Workers up mid-run
 * (docker compose up -d --scale worker=5) and watch both recover — that
 * before/after is the most persuasive thing you can put in front of a
 * customer, because it closes the loop from signal to action to result.
 */
export const options = {
  scenarios: {
    storm: {
      executor: 'ramping-arrival-rate',
      startRate: 10,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: 400,
      stages: [
        { target: 10, duration: '2m' },   // settle at a sane rate
        { target: 250, duration: '1m' },  // slam it
        { target: 250, duration: '5m' },  // hold — this is where you watch
        { target: 10, duration: '1m' },   // release
        { target: 10, duration: '3m' },   // recovery: watch the backlog drain
      ],
    },
  },
  // No thresholds. This scenario is SUPPOSED to hurt, and a red k6 summary
  // would imply the test failed when the test succeeded.
};

export function setup() {
  banner('Backlog storm — demand exceeds Worker capacity', [
    'Schedule-to-Start P99   → climbs well past the 200ms line',
    'Sync Match Rate         → drops below 95%',
    'Worker Task Slots       → floors at zero',
    'Frontend Request Rate   → shows the ramp',
    'RECOVERY: run `docker compose up -d --scale worker=5` during the hold',
  ]);
}

export default function () {
  startOrder({
    orderId: orderId('storm'),
    failureRate: 0,
    // 500ms per Activity against 10 activity slots per Worker caps a single
    // Worker at roughly 20 Activities/sec. At 250 orders/sec (3 Activities
    // each) one Worker is overwhelmed by well over an order of magnitude.
    activityDelayMs: 500,
    maxAttempts: 3,
  });
}
