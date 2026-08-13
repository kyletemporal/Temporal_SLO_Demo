# Temporal Self-Hosted — SLOs, Alerts & Golden Signals

A production-ready monitoring bundle for a self-hosted Temporal Service:
per-tenant SLOs with 28-day error budgets, burn-rate alerting, operational
alerts, and two Grafana dashboards.

No demo application, no load generator, no `auto-setup`. Drop these into an
existing Prometheus + Grafana and point them at a real cluster.

```
prometheus/
  slo-rules.yml            SLIs, error budgets, burn-rate alerts   (46 rules)
  alerts.yml               Operational alerts   (9 active + 1 commented)
  prometheus.example.yml   Scrape config: one target per service role
grafana/dashboards/
  temporal-golden-signals.json   RED + Saturation, with SLOs on top
  temporal-slo-board.json        Error budget wall + per-SLO table
docs/
  SLO-GUIDE.md             What is measured, why, and how to tune it
  SERVERLESS-WORKERS.md    Lambda: the pattern that works, and what it breaks
```

---

## Install

> **Do not load this alongside `demo/` in the same Prometheus.** Both record
> `slo:*` series with overlapping `sli` labels; together they produce duplicate
> series and silently wrong numbers. Running this alongside `cloud/` is fine —
> the Cloud bundle uses a `cloudslo:` prefix.


```bash
# 1. Rules
cp prometheus/slo-rules.yml prometheus/alerts.yml /etc/prometheus/
# add both to rule_files: in your prometheus.yml (see prometheus.example.yml)
promtool check rules /etc/prometheus/*.yml
curl -X POST http://prometheus:9090/-/reload

# 2. Dashboards — import via UI, or provision from disk
cp grafana/dashboards/*.json /var/lib/grafana/dashboards/temporal/
```

Both dashboards use a `DS_PROMETHEUS` datasource variable, so they work with
either UI import or file provisioning.

### Three things to change before this is yours

1. **`REPLACE_ME` in `alerts.yml`** — `TemporalWorkerFleetAbsent` needs a real
   namespace and task queue. Copy it once per business-critical Task Queue.
   Delete it entirely if those Workers are Serverless Workers: scale-to-zero
   makes absence the normal state and the rule will page continuously. See
   [`docs/SERVERLESS-WORKERS.md`](docs/SERVERLESS-WORKERS.md), which also covers
   the `task_delivery` SLI and why Prometheus cannot scrape Lambda.
2. **`REPLACE_ME` in `prometheus.example.yml`** — cluster name, worker label,
   datastore exporter.
3. **The objectives in `slo-rules.yml`.** They are placeholders. See below.

---

## Run it in recording-only mode first

**Do not route any of this to a pager on day one.** Load the rules, leave the
burn alerts unrouted, and watch for two weeks.

The objectives shipped here (99.9% availability, 99% latency) are round numbers,
not measurements. An SLO you miss at baseline is worse than no SLO: it trains
the team to ignore the board within a week. After two weeks you will know what
your cluster actually delivers, and you can promise something you can keep.

The SLI *definitions* are the part worth copying verbatim. Each one carries a
correction that was found by measurement, not by reading docs — see
`docs/SLO-GUIDE.md` §2.

---

## What is measured

| SLI | Tier | Per | Objective |
|---|---|---|---|
| `frontend_availability` | tenant | namespace | 99.9% |
| `frontend_latency` | tenant | namespace | 99% |
| `workflow_completion` | tenant | namespace | 99% |
| `task_delivery` | tenant | namespace | 99% |
| `history_availability` | infra | cluster | 99.9% |
| `matching_availability` | infra | cluster | 99.9% |
| `persistence_availability` | infra | service role | 99.9% |

**Tenant SLIs are per-namespace** — that is your isolation boundary and your
answer to "is it us or is it you". **Infra SLIs are shared**, because they are
your platform's health, not any one customer's.

Internal namespaces (`temporal_system`, `system`, `_unknown_`) are excluded
throughout. A tenant SLO that includes them measures Temporal's own housekeeping
as if it were customer traffic.

---

## Alerting model

Burn-rate alerts, not static thresholds. `error rate > 1%` fires identically for
a 90-second blip and a four-hour outage; a burn-rate alert fires in proportion
to how much budget the event is actually consuming, which is the same thing as
how much your users are actually suffering.

| Alert | Condition | Route |
|---|---|---|
| `SLOFastBurn` | 14.4x over 1h **and** 5m | page |
| `SLOSlowBurn` | 6x over 6h **and** 30m | page |
| `SLOBudgetBurnTicket` | 3x over 1d **and** 2h | ticket |
| `SLOBudgetBurnSlowTicket` | 1x over 3d **and** 6h | ticket |
| `SLOErrorBudgetExhausted` | budget ≤ 0 | ticket |

Two windows must breach together: the long one proves the burn is real, the
short one proves it is still happening, so alerts clear promptly instead of
lingering. **If you route tickets to the pager you have rebuilt threshold
alerting** and given up the benefit.

`alerts.yml` is separate and answers a different question — "is something broken
right now" rather than "are we still keeping the promise". Run both.

---

## Prerequisites that are easy to skip and expensive to miss

**Prometheus retention ≥ 28 days, on durable storage.** An error budget that
resets when the monitoring stack restarts will report perfect attainment through
an outage it has forgotten. This is the most common way SLO dashboards lie.

**Node-level metrics** (node_exporter / cAdvisor) alongside these. Two failure
modes produce an identical Temporal signature — schedule-to-start climbing, task
slots at zero — with opposite fixes. High Worker host CPU means add Workers; low
CPU means raise `MaxConcurrentActivityExecutionSize`. Host CPU is not a Temporal
metric, and without it those branches are a coin flip.

**Your datastore's own metrics.** Most self-hosted Temporal incidents are
datastore incidents. `persistence_latency` tells you Temporal is suffering; the
datastore exporter tells you why.

---

## Cost note

Layer 1 records 5m event rates; longer windows are derived with
`avg_over_time()` over those recordings rather than `rate(...[28d])` over raw
counters. That matters at scale — evaluating 28-day rates over raw series every
30 seconds is the standard way an SLO rule set takes down the monitoring system
it was meant to protect. It is also arithmetically better: because
`avg(rate) == events / window`, the derived ratio is traffic-weighted, whereas
averaging per-window ratios would weight a quiet Sunday equally with a Monday
peak.

Cardinality scales with namespaces. If you run hundreds of tenant namespaces,
consider restricting tenant SLIs to namespaces that carry a contractual SLO
rather than all of them.

---

## Version caveats

Built and verified against Temporal Server **1.26.x**, Prometheus **3.x**,
Grafana **11.5**. Two things drift between server versions and are worth
checking on yours:

- **`poll_success_sync`** varies more than any other metric name in this set.
  `docker exec <temporal> wget -qO- localhost:9090/metrics | grep poll_success`
- **The long-poll operation list.** `Poll.*` is not sufficient —
  `GetTaskQueueUserData` and `ListNexusEndpoints` are long-poll watches too. The
  discovery query is in `docs/SLO-GUIDE.md` §2.1; run it rather than trusting
  the list.

Also note: many Temporal metrics are **counters that do not exist until the
event they count occurs**. A metric reported "missing" on a quiet cluster is
usually unborn, not absent. Generate traffic before concluding anything is
broken.
