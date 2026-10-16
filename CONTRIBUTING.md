# Contributing

Thanks for looking. This repo is observability material for Temporal — rules,
dashboards, a runnable lab, and infrastructure-as-code — and it has a few
working rules that are easy to trip over if you have not read them.

> **Support posture:** see the note in [`README.md`](README.md#support-and-licence).
> That statement is under review as part of the repository transfer; until it is
> settled, assume issues are best-effort.

---

## The three rules that matter most

**1. Edit the generator, not the JSON.** Every dashboard in
`demo/grafana/dashboards/custom/` and several rule files are *generated* by
`tools/generate_*.py`. Hand-editing the output works until the next regeneration
silently reverts it. After changing a generator, regenerate and check for
`gridPos` overlaps.

```bash
python3 tools/generate_agent_dashboard.py
python3 tools/generate_scale_dashboard.py
```

**2. Never load `demo/` and `production/` into one Prometheus.** Both record
`slo:*` with overlapping `sli` labels, and loading both silently produces wrong
SLO numbers — no error, just wrong. `cloud/` and `app-team/` use distinct
prefixes and can coexist.

**3. Claims in docs are measured, not assumed.** This repo's documentation
states specific numbers ("95.9% of requests under 500ms", "8110 free slots for a
fleet that had 40"). Those came from running the stack. If you add a claim,
measure it and say what you measured it against — server version, SDK, and date.
A plausible number that nobody checked is worse than no number, because it gets
quoted.

---

## Before you open a pull request

Run the validation suite against a live stack. It is the only thing that
catches most classes of breakage here, because a broken dashboard panel and an
idle one look identical:

```bash
cd demo
make up                 # builds and starts everything
make baseline           # traffic — several checks need it
make agent-demo         # offline mock, no API key needed
make validate           # the actual gate
```

`make validate` must exit 0. Warnings are acceptable when they are explained
(no cAdvisor on this stack, OTel Python packages absent); failures are not.

For Go changes:

```bash
cd demo/app && go build ./... && go vet ./... && go test ./...
cd monitor  && go build ./... && go vet ./... && go test ./...
```

For the AI agent work specifically, the Go and Python implementations must keep
emitting the same telemetry contract:

```bash
cd demo && make agent-py-test
```

---

## What gets pushed back on

- **A dashboard panel with no data and no explanation.** If a panel is
  legitimately empty on a healthy system, add it to the allowlist in
  `demo/scripts/validate.sh` *with the reason*. An undocumented empty panel is
  indistinguishable from a broken one.
- **Two queries in one panel sharing a `refId`.** Grafana renders one and
  silently drops the other. `validate.sh` checks for this.
- **A threshold with no rationale.** [`RECOMMENDATIONS.md`](RECOMMENDATIONS.md)
  classifies every threshold as published guidance, measured here, structural,
  or a placeholder. New thresholds join that table.
- **Secrets, customer names, real namespace or account identifiers, or
  `*.tmprl.cloud` hostnames.** See [`SECURITY.md`](SECURITY.md). This repo is
  public.
- **Committed build artefacts.** A 58 MB compiled binary was tracked here for
  months before anyone noticed. `.gitignore` covers the known ones.

---

## Commit and PR style

Commit subjects say what changed and why it matters, in the imperative:

```
Fix the validator's template-variable resolver, which accused 23 working panels
```

not `fix validator`. The body is for the reasoning — especially the part a
reader would otherwise have to rediscover.

PRs should say what you ran, not just what you changed. "Verified with
`make validate` 46/0/1 on server 1.27.4" is the sentence a reviewer needs.

---

## Reporting security issues

Do **not** open a public issue for a vulnerability. See
[`SECURITY.md`](SECURITY.md) for the reporting process.
