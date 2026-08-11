import { startOrder, orderId, banner } from './lib/api.js';

/**
 * SCENARIO 2 — Failure injection
 *
 * Runs in two phases that look similar on an Activity failure chart and mean
 * completely different things.
 *
 *   Phase A (retries ON, maxAttempts 3): Activities fail, retries absorb it,
 *     Workflows still succeed. This is a HEALTHY system with a flaky
 *     dependency. Activity failure count is high; workflow_failed stays flat.
 *
 *   Phase B (retries OFF, maxAttempts 1): every Activity failure converts
 *     straight into a Workflow failure. Same Activity failure rate, terminal
 *     outcome.
 *
 * WHAT YOU ARE DEMONSTRATING
 * The failure conversion rate — workflow failures divided by activity
 * failures. Roughly:
 *
 *   < 0.01  → good resilience, retries are doing their job
 *   > 0.1   → poor error handling, failures are escaping to the Workflow
 *
 * Alerting on raw Activity failure count produces noise, because in a healthy
 * Temporal application Activity failures are expected and absorbed. The ratio
 * is the signal. Phase A is precisely the alert you do NOT want to page on;
 * Phase B is the one you do.
 */
export const options = {
  scenarios: {
    absorbed: {
      executor: 'constant-arrival-rate',
      rate: 25,
      timeUnit: '1s',
      duration: '5m',
      preAllocatedVUs: 25,
      maxVUs: 100,
      exec: 'retriesOn',
      startTime: '0s',
    },
    terminal: {
      executor: 'constant-arrival-rate',
      rate: 25,
      timeUnit: '1s',
      duration: '5m',
      preAllocatedVUs: 25,
      maxVUs: 100,
      exec: 'retriesOff',
      startTime: '5m',
    },
  },
};

export function setup() {
  banner('Failure injection — absorbed vs terminal failures', [
    'PHASE A (0-5m):  activity failures HIGH, Workflow Outcomes stays green',
    'PHASE B (5-10m): activity failures SAME, Workflow Outcomes goes red',
    'Workflow Outcomes       → the "failed" series appears only in phase B',
    'Frontend Errors by Type → should stay flat in BOTH phases',
    'The cluster is healthy throughout. This is an application signal.',
  ]);
}

// Phase A — 60% of payment attempts fail, but three attempts are allowed, so
// the probability a Workflow fails outright is 0.6^3, about 22%. Lower the
// failure rate to 0.3 and it falls to under 3%.
export function retriesOn() {
  startOrder({
    orderId: orderId('absorbed'),
    failureRate: 0.6,
    activityDelayMs: 20,
    maxAttempts: 3,
  });
}

// Phase B — same failure rate, no retries. Every failed Activity terminates
// its Workflow.
export function retriesOff() {
  startOrder({
    orderId: orderId('terminal'),
    failureRate: 0.6,
    activityDelayMs: 20,
    maxAttempts: 1,
  });
}
