# FR: Grafana Cloud + Temporal Cloud integration

**Status:** proposed · **Owner:** TBD · **Raised:** 2026-08-14

## Correct the premise first

There is **no "Temporal Cloud plugin"** in the Grafana plugin catalog — a search
of `grafana.com/api/plugins?keyword=temporal` returns nothing.

What exists is a **Grafana Cloud integration**: Grafana Cloud runs the scrape
itself against Temporal's OpenMetrics endpoint. Nothing is installed into
Grafana, and there is no data source plugin to configure. That difference
matters because it changes what you set up and what can break.

## What the integration actually does

Grafana Cloud → **Connections** → **Temporal Cloud** tile.

1. Add a scrape job: name, Temporal Cloud API key, scrape interval (default 1m).
2. **Test Connection**, then **Save**.
3. **Install** to deploy the prebuilt dashboards.

It runs entirely on Grafana Cloud infrastructure — no collector, no Prometheus,
no agent of ours. Metrics land labelled with the scrape job name. One job per
Temporal Cloud account; several accounts can feed one Grafana Cloud stack.

**Prerequisite:** a Service Account with the **Metrics Read-Only** account role,
which only an **Account Owner or Global Admin** can grant. A Namespace Admin
cannot complete this. Worth confirming before scheduling the work.

## Why we would do it

- Deletes the Prometheus we would otherwise run and retain for 28+ days purely
  to satisfy an error budget window.
- Ships a maintained Temporal dashboard we do not have to keep current.
- Adaptive Telemetry for cardinality and cost control, which matters because the
  v1 metrics carry `temporal_task_queue` and `temporal_workflow_type`.

## What it does NOT cover — the part that decides the design

**Cloud metrics only.** The integration scrapes `metrics.temporal.io`, which
stops at the Namespace boundary. It has no view of your Workers.

Temporal's own guidance is blunt about it: *"If you only ingest Cloud metrics,
you will miss many worker-side bottlenecks."* Slot exhaustion, schedule-to-start,
sticky cache behaviour, non-determinism errors — none of that is in Cloud
metrics, and three of the five SLIs in `app-team/` depend on it.

So adopting this is **not** "move everything to Grafana Cloud." It is:

| Signal | Path |
|---|---|
| Temporal Cloud service health | Grafana Cloud integration (serverless) |
| Worker SDK metrics | Alloy → Grafana Cloud Prometheus remote write |
| Logs | Alloy → Grafana Cloud Loki |

We already run Alloy in `demo/` for logs, so the collector is not new work — the
scope is adding a metrics pipeline to the Alloy config we have.

## Open questions

1. **Cost.** Scrape interval drives datapoints-per-minute and therefore bill.
   Default is 1m; we currently scrape at 30s. What do the v1 labels do to our
   active series count?
2. **Do our rules survive?** `cloud/prometheus/slo-rules.yml` is 39 rules of
   recording + burn-rate logic. Grafana Cloud supports Prometheus recording and
   alerting rules, but this needs confirming rather than assuming — the whole
   SLO model depends on it.
3. **Error budget retention.** A 28-day compliance window needs 28+ days of
   retention. Grafana Cloud's default retention against our plan?
4. **Does the prebuilt dashboard replace or duplicate ours?** Ours encodes
   corrections theirs may not — long-poll exclusion, client-fault exclusion,
   timeouts counted as bad. Likely both, clearly labelled.

## Known sharp edge

A Grafana Cloud user reported **Test Connection failing** while the same API key
worked fine via `curl`. Validate the key manually first, so a failure is
unambiguous:

```bash
curl -H "Authorization: Bearer <API_KEY>" https://metrics.temporal.io/v1/metrics
```

Expect output beginning `# TYPE temporal_cloud_v1_...`. Note `metrics.temporal.io`
is for scrapers, not browsers — opening it in a browser returns `Jwt is missing`.

## Proposed first step

Timebox a spike: one non-production Temporal Cloud namespace, one Grafana Cloud
scrape job, and answer question 2 — whether our 39 SLO rules load and evaluate
unchanged. If they do, the rest is configuration. If they do not, the migration
is materially larger than the integration page suggests.

## References

- <https://docs.temporal.io/cloud/metrics/openmetrics/metrics-integrations#grafana-cloud>
- <https://grafana.com/docs/grafana-cloud/monitor-infrastructure/integrations/integration-reference/integration-temporal/>
- <https://temporal.io/blog/monitoring-temporal-cloud-workflows-with-grafana-cloud>

---

## Related: upstream resources worth tracking

From Joshua Smith's *Temporal Cloud Observability* deck (July 2026):

- **Official Temporal observability skill** — <https://github.com/temporalio/skill-temporal-observability>
  A skill for coding agents instrumenting/querying/alerting on Temporal metrics.
  Worth diffing against this repo's rules rather than maintaining both blindly.
- **SDK alert pack** (~24 rules) — <https://github.com/tsurdilo/temporal-server-operations/tree/main/metrics/alerts/sdk>
  Covers ground this repo does not: NOT_FOUND on respond operations, all-pollers-disconnected,
  sticky cache disabled, local-activity latency exceeding WFT heartbeat timeout.
- Worker health — <https://docs.temporal.io/cloud/worker-health>
- Poller autoscaling, Worker autoscaling, Serverless Workers — the standard remedies
  for both "too few Workers" (schedule-to-start) and "too many" (poll success).
