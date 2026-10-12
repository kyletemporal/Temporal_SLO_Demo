## What and why

<!-- What changed, and the reasoning a reviewer would otherwise have to
     rediscover. If this fixes something subtle, say what the symptom looked
     like — that is the part that helps the next person. -->

## What you ran

<!-- Not what you changed — what you VERIFIED. Delete rows that do not apply. -->

- [ ] `cd demo && make validate` — result: <!-- e.g. 46 passed, 0 failed, 1 warning -->
- [ ] `go build ./... && go vet ./... && go test ./...` in `demo/app`
- [ ] `go build ./... && go vet ./... && go test ./...` in `monitor`
- [ ] `make agent-py-test` (if the GenAI telemetry contract changed)
- [ ] Regenerated dashboards and checked for `gridPos` overlaps (if a generator changed)

Tested against: Temporal Server <!-- version -->, SDK <!-- language + version -->

## Checklist

- [ ] I edited the **generator**, not generated JSON (see CONTRIBUTING.md)
- [ ] Any new threshold is classified in `RECOMMENDATIONS.md`
- [ ] Any panel that is legitimately empty is allowlisted in `validate.sh` **with a reason**
- [ ] No secrets, customer names, real namespace/account identifiers or `*.tmprl.cloud` hosts
- [ ] No committed build artefacts
- [ ] Numbers quoted in docs were measured, and I said what against
