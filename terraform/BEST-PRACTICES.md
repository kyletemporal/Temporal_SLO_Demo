# Temporal Cloud + Terraform: practices worth enforcing

Each rule below is encoded in the modules. The reasoning matters more than the
rule, because you will hit cases the modules do not cover.

---

## 1. Once Terraform owns a resource, only Terraform changes it

Temporal's own guidance is blunt about this, and the failure mode is specific: a
change made in the Cloud UI becomes **drift**, and the next `terraform apply`
silently reverts it. Someone granting emergency access through the UI at 2am
loses it at the next pipeline run.

Import what already exists rather than leaving it half-managed:

```bash
terraform import temporalcloud_namespace.orders orders.<account-id>
terraform import temporalcloud_user.lead      <user-id>   # tcld user list
```

User IDs are not shown in the Cloud UI — get them from `tcld user list`.

---

## 2. Namespace access attaches to the IDENTITY, not the Namespace

There is no way to grant access from `temporalcloud_namespace`. Permissions live
on `temporalcloud_user` and `temporalcloud_service_account` via
`namespace_accesses`. Expecting the reverse is the most common first-time
mistake, and it shapes how you lay files out.

**Consequence: manage each identity in exactly one place.** Terraform writes the
*complete* permission set on every apply, so the same user declared in two
modules gets their access overwritten by whichever applies last — no error, no
warning. One user, one resource block, one file.

Account Owners and Global Admins implicitly have every Namespace, so
`namespace_accesses` is meaningless for them.

---

## 3. Terraform cannot manage the Account Owner

You can import it; you cannot create, update or delete it. `team-onboarding`
enforces this with a `precondition` so the failure is a clear message at plan
time rather than a confusing API error at apply.

---

## 4. API key tokens land in state — plan for it

Terraform must know the value it created, so the token is in state as plaintext.
`sensitive = true` only redacts it from *output*; it is not encryption.

- Use a remote backend with encryption at rest and tight access control.
- Never commit `terraform.tfstate` or `*.tfstate.backup`.
- Read the value deliberately (`terraform output -raw ...`) into a secret
  manager, and do not echo it in CI.
- If you cannot secure state properly, create keys with `tcld` instead and
  reference them — managing them in Terraform is then a net loss.

---

## 5. Expiry is a feature, not an obstacle

`expiry_time` is required and there is no "never expires". An expiry you must
renew is a rotation you cannot forget.

The failure mode is worth designing around: when a key expires, **Workers stop
connecting and metrics silently stop arriving**. A stopped metrics pipeline looks
identical to a healthy quiet system on a dashboard — the same class of silent
failure the [`monitor/`](../monitor) service guards against with poll-freshness
alerting. Alert on scrape staleness as well as diarising the renewal.

---

## 6. Least privilege is expressible, so express it

`account_access` accepts `admin`, `developer`, `read`, `financeadmin` and
`metricsread`. That last one exists specifically so a metrics scraper is not an
account admin.

- A workload identity usually needs **no account role at all** — only
  `namespace_accesses`. `team-onboarding` omits `account_access` deliberately.
- A metrics scraper gets `metricsread`. Nothing else.
- Do not set `account_access` together with `namespace_scoped_access`; the
  provider rejects it.

---

## 7. Guard the irreversible things

- **`prevent_destroy` on Namespaces.** Deleting one destroys every Workflow
  history in it, unrecoverably. `name` forces replacement, so a typo in a
  variable would otherwise queue a destroy/create of production.
- **Regions cannot change after creation.** Changing them means a new Namespace
  and a migration.
- **`retention_days` is the ceiling on any Visibility-based SLO window.** You
  cannot compute 28-day compliance from 14 days of history.

---

## 8. Set generous timeouts

Namespace creation takes minutes — Temporal's own example shows 2m17s, and
multi-region is slower. The default is frequently too short, and a timeout
mid-create leaves state and reality disagreeing. The modules default to 15m.

---

## 9. Declare search attributes up front

They are how anything finds Workflows later, including the duration SLOs in
[`monitor/`](../monitor). Adding them early is free. Discovering mid-incident
that you cannot filter by `CustomerTier` is not recoverable in the moment.

---

## 10. Pin the provider, and read its schema rather than the tutorial

```hcl
temporalcloud = { source = "temporalio/temporalcloud", version = "~> 1.7" }
```

The published docs lag the provider by two major versions. Building this
directory turned up three things the tutorials would have got wrong: resources
that exist but are undocumented, a resource that provisions a **deprecated**
endpoint, and block syntax that no longer compiles. `terraform providers schema
-json` is the authority.

---

## 11. Validate in CI, even without credentials

`terraform init -backend=false` and `terraform validate` need no API key, and
they catch schema and type errors — including the block-vs-attribute change that
broke two of these examples on first write. Add `terraform fmt -check -recursive`
so review is about substance rather than whitespace.
