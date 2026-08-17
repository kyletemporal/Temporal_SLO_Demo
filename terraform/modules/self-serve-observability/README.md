# Self-serve observability

Lets application teams provision their own Grafana folder, dashboard, alerts and
paging from ~20 lines of tfvars, so the platform team is not the bottleneck for
everyone else's observability — and so the standard does not drift team by team.

See [`../../examples/04-self-serve`](../../examples/04-self-serve) for what a team
actually submits.

## VERIFY THIS YOURSELF BEFORE ROLLING IT OUT

This module passes `terraform validate` and its dashboard template has been
rendered and parsed as JSON. **It has never been applied**, because that needs a
live Grafana. Validation proves the configuration is well-formed; it does not
prove Grafana accepts it or that a page reaches a human.

Run these four checks against a **non-production Grafana** first. Each targets a
failure that is silent — it applies cleanly and looks green while being wrong.

### 1. The no-contact-method guard actually fires

The worst failure this module can have: a contact point with **zero
integrations**. It applies successfully, shows green, and routes every alert to
nowhere. A `precondition` is supposed to stop that at plan time.

```bash
# Deliberately omit BOTH slack_webhook_url and email_addresses, then:
terraform plan
```

**Expect:** the plan FAILS with *"At least one contact method is required…"*.
If it plans cleanly, the guard is not working — do not roll out until it does.

### 2. The dashboard renders in Grafana, not just as JSON

```bash
terraform apply
# then open the folder URL from the output
```

**Expect:** four panels with data, no "Datasource not found", and PromQL that is
correctly quoted. The selectors are JSON-escaped in `locals` because a raw
selector produces invalid JSON — verified by rendering, but only Grafana can
confirm it renders as a *dashboard*.

### 3. An alert actually pages a human

The most important check, and the one most often skipped. Provisioning a contact
point is not evidence that a message arrives.

```bash
# In Grafana: Alerting → Contact points → your team's point → Test
```

**Expect:** a message in the Slack channel or inbox. Then force a real alert —
`cd demo && make chaos-backlog` drives schedule-to-start up — and confirm the
page arrives with the runbook link attached.

### 4. Your SDK's labels and units match

The alerts assume Go/Java conventions by default.

```bash
cd demo && make verify-sdk-labels
```

**Expect:** PASS on both the label name and the units. If your teams run
TypeScript, Python or .NET, set `sdk_emits_seconds = false` — the metric NAME and
the threshold arithmetic both change, and getting it wrong is a silent 1000×
error. The non-determinism rule selects on `failure_reason`, verified on Go via
tally only.

## Why there is no `grafana_notification_policy` here

That resource manages the **entire** notification policy tree and overwrites it.
A per-team copy would make each team's apply silently erase every other team's
routing.

Instead each alert rule carries `notification_settings.contact_point`, routing
directly to the team's own contact point. The shared tree stays owned by the
platform team, and any number of teams can apply this module without colliding.

## What the team controls, and what it does not

| Team chooses | Platform owns |
|---|---|
| Namespace and task queues | Alert thresholds |
| Where to page | Which alerts exist |
| SDK language (units) | The dashboard layout |
| Runbook link | Folder naming |

Thresholds are deliberately not team-configurable. A team that needs different
numbers should be talking to the platform team, and requiring that conversation
is the point — otherwise this is a template, not a standard.

## Known behaviours worth knowing

- **Leaving `grafana_team_ids` empty skips the permission resource entirely**, so
  the folder inherits Grafana's defaults rather than being locked down. Safe for
  a first apply (it cannot lock you out), but not the end state.
- **Folder permissions are a complete set** — anything not declared is removed.
  The Viewer role is listed explicitly for that reason.
- **`prevent_destroy_if_not_empty`** guards the folder, so removing a module block
  will not quietly delete dashboards someone still uses.
