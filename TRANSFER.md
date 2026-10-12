# Transfer checklist

This repository was developed under personal ownership and is being transferred
to Temporal. This file tracks what that requires. **Delete it once the list is
clear** — it is a transfer artefact, not documentation.

The CI job `hygiene / no unresolved transfer placeholders` fails while any
`REPLACE_ME(...)` marker remains, so nothing on the "decisions" list can ship
silently.

---

## Done

- [x] **LICENSE reassigned.** `Copyright (c) 2026 Kyle Shelton` →
      `Temporal Technologies Inc.` MIT retained.
- [x] **Go module paths made real.** Both were unresolvable:
      `github.com/example/temporal-obs-demo` and
      `github.com/temporal-slo-demo/monitor`. Now
      `github.com/temporalio/temporal-slo-demo/{demo/app,monitor}`.
      **Assumes the repo lands at `temporalio/temporal-slo-demo`** — if it gets
      a different name, this is one `sed` across `*.go` and `go.mod`, and both
      modules must still build.
- [x] **58 MB binary untracked.** `demo/app/temporal-obs-demo`, a macOS arm64
      build that nothing needs — `demo/app/Dockerfile` builds its own from
      source. **It remains in git history**; see "Open questions" below.
      Note the trap: `go build` names its output after the module's last path
      element, so renaming the module to `.../demo/app` changed the artefact
      name from `temporal-obs-demo` to `app` — and a gitignore listing only the
      old name let a fresh 58 MB binary reappear untracked within the hour.
      `.gitignore` now anchors both names, plus the `monitor/` equivalents. A
      CI job fails on any committed binary regardless.
- [x] **Customer-engagement references removed.** `TASKS.md` referred to
      "(customer's version)", "the customer's teams", and had a section headed
      "Open questions for the customer" — engagement framing that reads as an
      account artefact on a public repo. Generic uses ("a Cloud customer has
      nothing between…") were left; they are correct.
- [x] **Named individual removed.** A colleague was credited by name in
      `docs/FR-task-accepted-latency.md` and `TASKS.md` for the question behind
      the task-acceptance FR. Attribution is a courtesy that becomes a
      disclosure once the org owns the page.
- [x] **Go sources gofmt'd.** Five files in `demo/app` were not, which the new
      CI would have failed on.
- [x] **Governance files added** — `CONTRIBUTING.md`, `SUPPORT.md`,
      `CODE_OF_CONDUCT.md`, `CODEOWNERS`, PR and issue templates.
- [x] **CI added** (`.github/workflows/ci.yml`): build/vet/test/gofmt on both
      Go modules, `govulncheck` (non-blocking), generator reproducibility,
      shell syntax, YAML/JSON parse, `promtool check rules` on all 12 rule
      files, and hygiene gates for placeholders, committed binaries and
      secrets. Every job was run locally first.
- [x] **CodeQL added** — Go, Python and Actions, weekly plus on push.
- [x] **Dependabot added** — both Go modules, pip, three Dockerfiles, Actions.
      This exists because of a real miss: see SECURITY.md P8b.

---

## Decisions needed — each marked `REPLACE_ME(...)` in the file

**1. Support posture** — `README.md`, `SUPPORT.md`
Five places currently say this is **not** a Temporal product and that issues
should not be raised as support tickets. True yesterday, false the moment the
org owns it. Someone who owns support policy picks one:
supported product / official sample, best-effort / official but explicitly
as-is. Everything else follows from that choice.

**2. Disclosure policy** — `SECURITY.md` §6
GitHub renders `SECURITY.md` as *the* vulnerability-reporting policy. It
currently says "contact the maintainer directly" and "no bounty is offered" —
both must route into Temporal's real process and must not contradict any
bounty programme. The routing for bugs in Temporal itself and in upstream
components is already correct and should survive the rewrite.

**3. Code of Conduct** — `CODE_OF_CONDUCT.md`
Placeholder is the Contributor Covenant 2.1. Replace with the org-standard
text, and the reporting address **must** be a monitored organisational inbox.

**4. CODEOWNERS handles** — `CODEOWNERS`
Every handle is a placeholder. An unresolvable team silently assigns nobody,
which looks like review coverage and is not. Fix before enabling branch
protection.

**5. Issue-template security link** — `.github/ISSUE_TEMPLATE/config.yml`
Points at `github.com/REPLACE_ME/security/policy`.

---

## Open questions — need a human with the right hat

**Grafana Labs derivative work.** `demo/grafana/dashboards/custom/temporal-full-overview.json`
is a rebuild of Grafana Cloud's "Temporal overview" dashboard;
`docs/CLOUD-TO-SELFHOSTED.md` names the source and documents what was
translated, replaced and dropped. It was rebuilt rather than copied, and the
schema, metric names and query bodies all differ. Whether that needs a `NOTICE`
entry or attribution is a legal question, and cheaper to answer before transfer
than after. There is currently no `NOTICE` file.

**Binary in git history.** Untracking `demo/app/temporal-obs-demo` stops future
churn but does not remove ~58 MB from history. Rewriting history is
straightforward *now* and disruptive later — but it invalidates every existing
clone and SHA. Decide before transfer, not after.

**AI-assisted authorship.** 38 commits carry `Co-Authored-By: Claude` trailers.
Whatever Temporal's disclosure policy is, applying it is far cheaper before
transfer than rewriting history afterwards.

**Security self-assessment.** `SECURITY.md` holds 20 findings, 2 High still
open (Docker socket in Alloy; 14 unauthenticated published ports) plus default
`admin`/`admin` credentials — all correctly reasoned as lab-only. But a risk
register authored by the repo's own author reads differently than one signed
off by security. Expect a review, not a rewrite.

**Criticism of Temporal's shipped artefacts becomes first-party.** `README.md`
states the shipped `TemporalFrontendLatencyHigh` alert "fired **forever** on a
healthy idle cluster", and `docs/FR-*.md` are product asks with "Owner: TBD".
All accurate and evidenced — but the register changes when the publisher is
Temporal. Worth a product read.

**Stale claim.** `TASKS.md` and `TESTING.md` still quote "**37/37, 0 warnings**"
from before the agent and scale work. The current measured figure is
**46 passed, 0 failed, 1 warning** (2026-09-28, recorded in TASKS.md). The old
figure was left in the historical sections deliberately — they are dated
records — but do not quote it as current.
