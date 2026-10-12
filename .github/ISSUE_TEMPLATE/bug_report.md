---
name: Bug report
about: Something in this repository does not work as documented
labels: bug
---

## What happened

<!-- Including, if a dashboard is involved, the panel title and the query it ran. -->

## What you expected

## Environment

- Temporal Server version:
- SDK and language:
- Deployment: <!-- demo/ compose stack, self-hosted, Temporal Cloud -->
- Grafana / Prometheus versions (if relevant):

## `make validate` output

<!-- If the demo stack is involved, paste the relevant section. It distinguishes
     "empty by design" from "no traffic yet" from actually broken, which is
     usually the whole answer. -->

## Before filing

- [ ] I checked `demo/SETUP.md` troubleshooting
- [ ] If an alert misbehaves, I read its entry in `RECOMMENDATIONS.md`
- [ ] If labels or units look wrong, I ran `make verify-sdk-labels`
- [ ] This is **not** a security vulnerability (those go via `SECURITY.md`, never a public issue)
