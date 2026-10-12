# Support

<!-- REPLACE_ME(support-posture): This file states where help comes from, and it
     must agree with README.md and SECURITY.md §6. All three were written while
     the repo was personally owned. Settle the posture once, then make all three
     say the same thing. Do not ship with this comment still present. -->

**This repository is under transfer to Temporal ownership, and its support
posture is being settled as part of that.** Until it is, treat everything here
as best-effort.

## Where to ask

| Your question is about | Go to |
|---|---|
| Something in **this repository** — a dashboard, a rule, the demo stack, the monitor service, the Terraform modules | Open an issue here |
| **Temporal Server, the SDKs, or Temporal Cloud** | [Temporal community forum](https://community.temporal.io/) or [Temporal support](https://support.temporal.io/), depending on your plan. Not here. |
| A **security vulnerability** | [`SECURITY.md`](SECURITY.md). Never a public issue. |
| **Grafana, Prometheus, Loki, Tempo, Pyroscope, the OTel Collector** | Upstream. If it also needs a change here — a version bump, a config default — an issue here is welcome too. |

## Before opening an issue

Most reports here come down to one of a handful of things, and the answers are
already written down:

- **A dashboard panel is empty.** Several are empty by design on a healthy
  system. `demo/scripts/validate.sh` lists which and why. Run `make validate` —
  it distinguishes "empty by design", "no traffic yet", and actually broken.
- **An alert never fires / fires constantly.** Read
  [`RECOMMENDATIONS.md`](RECOMMENDATIONS.md). Every threshold in this repo is
  classified there, and several ship as deliberate `REPLACE_ME` placeholders.
- **The alerts do not match your SDK.** Run `cd demo && make verify-sdk-labels`.
  Label names and latency units differ by SDK language — Go and Java emit
  seconds, TypeScript, Python and .NET emit milliseconds, and the published
  non-determinism rule uses a label the Go SDK does not emit.
- **The stack will not start.** [`demo/SETUP.md`](demo/SETUP.md) has a
  troubleshooting section covering the common failures.

## What to include

- What you ran and what you expected.
- Temporal Server version, SDK and language, and how you are deploying.
- `make validate` output if the demo stack is involved.
- For a dashboard issue: the panel title and the query it ran.
