import { startOrder, orderId, banner } from './lib/api.js';

/**
 * SCENARIO 8 — Poller flood (over-provisioned Worker fleet)
 *
 * THE LOAD IS NOT THE CHAOS. THE FLEET IS.
 *
 * This script on its own is a quiet, healthy baseline — roughly 0.2 orders/sec
 * with fast Activities and no failures, and every threshold below is expected
 * to PASS. The scenario is produced by `make chaos-poller-flood`, which scales
 * the Worker fleet to 20 replicas underneath this load. Running this file alone
 * gives you the reference shape, not the finding.
 *
 * WHAT YOU ARE DEMONSTRATING
 * Scenarios 0-7 are all STARVATION shapes: not enough Workers, not enough
 * slots, no Workers, or nothing moving. This is the only one that runs the
 * fleet in the opposite direction, and it inverts the fix.
 *
 * Over-provision the fleet relative to offered load and most long-polls wait
 * their full 60s and return empty. `poll_timeouts` swamps `poll_success`, so
 * POLL SUCCESS RATE COLLAPSES THROUGH ANY REASONABLE THRESHOLD WHILE THE
 * SYSTEM IS ENTIRELY HEALTHY:
 *
 *   sync match rate      ~100%   (a Worker was always waiting)
 *   schedule-to-start    ~0      (nothing waited for a Worker)
 *   slots available      free    (nothing is saturated)
 *   workflow outcomes    success (nothing failed)
 *   poll success rate    COLLAPSES
 *
 * WHY IT MATTERS
 * A starved fleet and a flooded fleet BOTH push poll success rate down, and
 * they need opposite responses. Anyone alerting on that metric alone will add
 * Workers during a flood — which makes it worse, and costs money doing it.
 *
 * This is the same mechanism as the `TemporalFrontendLatencyHigh` false
 * positive in the root README: a long-poll blocks for 60s BY DESIGN. There it
 * poisons a latency percentile; here it collapses a success ratio. An empty
 * long-poll is not a failure, it is a poller that had nothing to do.
 *
 * THREE THINGS THIS PROVES THAT THE REPO OTHERWISE ONLY ASSERTS
 *   1. Empty long-polls are not failures.
 *   2. Poll success rate cannot page anyone without a second, independent
 *      condition. `TemporalMatchingStarved` adds schedule-to-start and stays
 *      silent through this entire run — that is its acceptance test.
 *   3. `poll_timeouts` is NOT an async-match counter. Temporal has no async
 *      match metric; async match is `poll_success - poll_success_sync`.
 *      Reading `poll_timeouts` as async match inverts the conclusion.
 *
 * CLEANUP IS REQUIRED
 *   make chaos-poller-flood-reset
 * Twenty Workers left running will absorb the backlog in scenarios 1 and 4 and
 * make them look like they failed to reproduce.
 */
// ORDER_RATE is orders/SECOND and is routinely below 1 for this scenario — the
// whole point is a fleet that dwarfs the offered work.
//
// k6's constant-arrival-rate `rate` MUST BE AN INTEGER. A fractional value is
// not clamped or rounded, it aborts the run before a single request:
//
//   could not initialize '/scripts/08-poller-flood.js': json: cannot unmarshal
//   number 0.3 into Go struct field Options.scenarios of type int64
//
// Worse, driven from a shell script that error scrolls past and the run looks
// like it simply produced no load — which reads as "the scenario does not
// reproduce" rather than "the script never started". Sub-1 rates are therefore
// expressed as an integer count per 10s.
const PER_SEC = Number(__ENV.ORDER_RATE || 0.2);
const SUB_HZ = PER_SEC < 1;
const RATE = SUB_HZ ? Math.max(1, Math.round(PER_SEC * 10)) : Math.round(PER_SEC);
const TIME_UNIT = SUB_HZ ? '10s' : '1s';

export const options = {
  scenarios: {
    quiet: {
      executor: 'constant-arrival-rate',
      // Deliberately low. The ratio between pollers and offered work is what
      // produces the effect, and this is the denominator.
      rate: RATE,
      timeUnit: TIME_UNIT,
      duration: __ENV.DURATION || '10m',
      preAllocatedVUs: 10,
      maxVUs: 50,
    },
  },
  // These MUST pass. The application side being perfectly healthy while a
  // cluster ratio collapses is the entire point of the scenario — a red k6
  // summary here would undercut the finding.
  //
  // Plain 'checks', not 'checks{}'. k6 0.55 parses an empty tag expression as
  // an empty metric name and refuses to start the run at all.
  thresholds: {
    checks: ['rate>0.99'],
    'http_req_duration{name:POST /orders}': ['p(95)<500'],
  },
};

export function setup() {
  banner('Poller flood — healthy fleet, collapsing poll success rate', [
    'Poll Outcome Mix        → EMPTY band grows ~23x (0.03 -> 0.66/s)',
    'Poll Success Rate       → collapses. This is the ONLY thing that moves.',
    'Sync Match Rate         → stays ~100%. Does NOT follow it down.',
    'Schedule-to-Start P99   → stays near zero',
    'Worker Task Slots       → stay free',
    'Workflow Outcomes       → success only',
    '',
    'TemporalMatchingStarved must NOT fire — that is the acceptance test.',
    'AFTERWARDS: make chaos-poller-flood-reset',
  ]);
}

export default function () {
  startOrder({
    orderId: orderId('flood'),
    failureRate: 0,
    // Short Activities so slots free up immediately and pollers go straight
    // back to waiting. A slow Activity would occupy slots and start producing
    // the STARVATION shape this scenario exists to contrast against.
    activityDelayMs: 50,
    maxAttempts: 3,
  });
}

export function handleSummary(data) {
  const checks = data.metrics.checks;
  const rate = checks ? (checks.values.rate * 100).toFixed(2) : 'n/a';

  const out = `
========================================================================
  SCENARIO 8 COMPLETE — poller flood
========================================================================
  Orders accepted OK : ${rate}%   (expected ~100% — the app is healthy)

  GO LOOK AT, on the Golden Signals board:

    Poll Outcome Mix
      The EMPTY band should grow sharply — measured 0.03/s -> 0.66/s,
      about a third of the whole stack. Every poll ends in exactly one
      of sync / async / empty, so the stack is the complete picture.

    Sync Match Rate vs Poll Success Rate
      They should DIVERGE. Sync match flat near 100%, poll success on the
      floor. Two ratios that look alike and measure unrelated things:
      one is a health signal, the other is a SIZING signal.

    Discriminator (schedule-to-start P99 + slots available)
      Both should look completely healthy. That is what separates this
      from scenarios 1 and 4, which move poll success rate the same way
      for the opposite reason.

  CHECK THE ALERTS:
    TemporalMatchingStarved              must NOT fire
    TemporalWorkerFleetOverProvisioned   should reach pending/firing (info)

------------------------------------------------------------------------
  CLEANUP IS REQUIRED:   make chaos-poller-flood-reset

  Leaving 20 Workers running will absorb the backlog in scenarios 1 and
  4 and make them look like they failed to reproduce.
========================================================================
`;
  return { stdout: out };
}
