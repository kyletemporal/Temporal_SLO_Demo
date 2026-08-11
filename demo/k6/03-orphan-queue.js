import { startOrder, orderId, banner } from './lib/api.js';

/**
 * SCENARIO 3 — Orphaned Task Queue
 *
 * Starts Workflows on a Task Queue that no Worker polls, simulating the single
 * most common Temporal misconfiguration: a typo or environment drift between
 * the Task Queue name a Workflow starter uses and the one a Worker registers.
 *
 * WHAT YOU ARE DEMONSTRATING
 * Why "Tasks With No Poller" earns its place on a minimal dashboard.
 *
 * Every other backlog signal on the board is ambiguous. High schedule-to-start
 * latency could be capacity, configuration, or a slow dependency. Low sync
 * match could be any of several things. no_poller_tasks has essentially no
 * false-positive mode — a sustained non-zero value means work is queuing
 * somewhere nobody is listening, and it will never drain on its own.
 *
 * It is also the failure that scaling makes WORSE, not better: add ten more
 * Workers to the correct queue and the orphaned queue stays exactly as stuck
 * as it was. That is the point worth landing with the customer.
 *
 * NOTE ON VOLUME: this runs at a low rate deliberately. Every Workflow started
 * here is stranded until its 10-minute execution timeout fires. There is no
 * reason to strand thousands to prove the point.
 */
export const options = {
  scenarios: {
    orphan: {
      executor: 'constant-arrival-rate',
      rate: 5,
      timeUnit: '1s',
      duration: __ENV.DURATION || '4m',
      preAllocatedVUs: 10,
      maxVUs: 30,
    },
  },
};

export function setup() {
  banner('Orphaned Task Queue — work sent where no Worker listens', [
    'Tasks With No Poller    → goes non-zero and STAYS non-zero',
    'Sync Match Rate         → unaffected (healthy queue is still healthy)',
    'Schedule-to-Start P99   → unaffected (no SDK metrics on a queue with no Worker)',
    'Frontend Availability   → unaffected. The cluster is fine.',
    'KEY POINT: scaling Workers does NOT fix this. Only fixing the name does.',
  ]);
}

export default function () {
  startOrder({
    orderId: orderId('orphan'),
    // No Worker registers this queue. Compare with the "orders" queue that
    // the worker service polls.
    taskQueue: 'orders-ghost',
    failureRate: 0,
    activityDelayMs: 10,
    maxAttempts: 1,
  });
}
