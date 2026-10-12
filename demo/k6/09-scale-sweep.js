import { startOrder, orderId, banner } from './lib/api.js';

/**
 * SCENARIO 9 — Scale sweep driver
 *
 * NOT A CHAOS SCENARIO. Scenarios 0–8 break something and ask what the
 * dashboards show. This one breaks nothing: it holds a FIXED, KNOWN offered
 * load while scripts/scale-benchmark.sh changes the worker count underneath it,
 * so that throughput and latency can be attributed to fleet size rather than to
 * a moving load generator.
 *
 * WHY A FIXED RATE INSTEAD OF RAMPING VUs
 *
 * This is the single most common way a Temporal load test produces a
 * meaningless number. A VU-based test is CLOSED-LOOP: each VU waits for its
 * request to finish before sending the next, so when workers slow down the
 * generator slows down with them. Offered load becomes a function of the
 * system's own capacity, throughput looks flat no matter what you do, and the
 * backlog that would reveal the bottleneck never forms.
 *
 * constant-arrival-rate is OPEN-LOOP: k6 sends N requests per second whether or
 * not anything is keeping up. That is what makes the queue grow, and the queue
 * growing is the entire measurement.
 *
 * INTEGER RATES ONLY. k6 0.55 unmarshals `rate` into an int64 and aborts the
 * whole run on a fractional value — "cannot unmarshal number 0.3 into Go struct
 * field Options.scenarios of type int64". For sub-1 rates, express them as an
 * integer over a longer timeUnit (`rate: 3, timeUnit: '10s'`).
 *
 * Usage — normally invoked by scripts/scale-benchmark.sh, not by hand:
 *
 *   docker compose run --rm \
 *     -e RATE=40 -e DURATION=3m -e ACTIVITY_DELAY_MS=50 \
 *     k6 run /scripts/09-scale-sweep.js
 */
const RATE = parseInt(__ENV.RATE || '40', 10);
const DURATION = __ENV.DURATION || '3m';
const ACTIVITY_DELAY_MS = parseInt(__ENV.ACTIVITY_DELAY_MS || '50', 10);

export const options = {
  scenarios: {
    sweep: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      // Both generously sized relative to RATE. If k6 runs out of VUs it
      // silently drops below the requested arrival rate and logs a warning
      // nobody reads — and then the benchmark's "offered load" column is a
      // lie, which corrupts every derived figure in the report.
      preAllocatedVUs: Math.max(50, RATE * 2),
      maxVUs: Math.max(200, RATE * 10),
    },
  },
  // NO THRESHOLDS, deliberately.
  //
  // A threshold makes k6 exit non-zero, and this scenario is EXPECTED to be run
  // at rates the fleet cannot sustain — that is how the saturation point gets
  // found. Failing the run at saturation would abort the sweep exactly when it
  // starts producing the interesting data.
  //
  // Whether a given rate was sustainable is decided afterwards from Prometheus,
  // by scripts/scale-benchmark.sh.
  thresholds: {},
};

export function setup() {
  banner(`Scale sweep — ${RATE}/s for ${DURATION}`, [
    'Workflow completion rate  → does it match the offered rate?',
    'Schedule-to-Start P95     → the first thing to move when saturated',
    'Worker Task Slots         → zero means slot-bound, not worker-bound',
    'Sync Match Rate           → falls as the queue stops draining instantly',
    'Host CPU / container CPU  → separates CPU-bound from slot-bound',
  ]);
  console.log(`  offered load: ${RATE}/s · activity delay: ${ACTIVITY_DELAY_MS}ms`);
  console.log('');
}

export default function () {
  startOrder({
    orderId: orderId('scale'),
    failureRate: 0,
    // A NON-ZERO delay is load-bearing for this measurement.
    //
    // With instant activities, a laptop saturates on Temporal's own RPC path
    // long before worker slots matter, and the sweep measures the server
    // instead of the fleet. A 50ms activity means one slot sustains ~20
    // activities/sec, so the capacity arithmetic in docs/SCALE-TESTING.md has
    // a real quantity to predict and be checked against.
    activityDelayMs: ACTIVITY_DELAY_MS,
    maxAttempts: 3,
  });
}
