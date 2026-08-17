# Temporal + Terraform design patterns

Four patterns, each a working configuration under [`patterns/`](patterns) that
passes `terraform validate`. Each solves a problem that shows up once Temporal
Cloud is used by more than one team.

The pattern is the *reasoning*. Copy the shape, not the names.

---

## 1. Environment promotion — [`patterns/01-environment-promotion`](patterns/01-environment-promotion)

**Problem.** Dev, staging and prod need the same Namespace shape with different
regions, retention and size. The obvious approach — a directory per environment —
produces a fix applied to prod that never reaches staging, and a staging config
that has quietly stopped resembling prod.

**Shape.** One configuration, per-environment `.tfvars`, separate state.

```bash
terraform apply -var-file=envs/prod.tfvars
```

**What the pattern encodes**

- **Environment is part of the Namespace name**, not just a tag. A Namespace you
  cannot identify at a glance is how the wrong one gets terminated at 2am.
- **Retention is derived from the environment**, because it is the ceiling on any
  Visibility-based SLO window. Nobody remembers that a 28-day compliance window
  needs 28+ days of history until it silently cannot be computed.
- **The prod/dev difference lives in data**, not in a forked copy of the config.

**Workspaces or separate directories?** Workspaces share a backend and a set of
credentials — convenient, and precisely the argument against them for production,
because an operator with staging access can `workspace select prod`. When blast
radii differ, use separate backends and separate credentials.

---

## 2. Cross-team service boundaries — [`patterns/02-nexus-cross-team`](patterns/02-nexus-cross-team)

**Problem.** Two teams need to call each other's Workflows. Sharing a Namespace or
handing out cross-namespace credentials couples their failure domains and their
access control permanently.

**Shape.** A Nexus Endpoint. The provider Namespace exposes it; caller Namespaces
are named explicitly. Teams keep separate Namespaces, retention and blast radius.

**Why this belongs in Terraform.** `allowed_caller_namespaces` **is** the access
control list. In a pull request it is a diff someone approves; done by hand in the
UI it is an access grant with no record of who asked or why.

Note what the provider makes impossible: a caller cannot grant itself access from
its own configuration. Only the provider Namespace's config can change that list,
which puts the decision with the team that carries the risk.

Also worth writing properly: the endpoint `description` is the contract. It is the
first thing a caller team reads, and unlike a wiki page it is versioned with the
grant.

---

## 3. Credential rotation without downtime — [`patterns/03-credential-rotation`](patterns/03-credential-rotation)

**Problem.** API keys must expire — `expiry_time` is required and there is no
"never". Replacing a key in place kills the old one the instant Terraform
applies, and every Worker still holding it fails to connect. Rotation done that
way is an outage you scheduled yourself.

**Shape.** Two keys exist at once. Three applies, each individually reversible:

| Step | Change | State |
|---|---|---|
| 1 | `active_key_slot = "a"` | Both keys exist, Workers use A |
| 2 | `active_key_slot = "b"` | Both still exist, Workers move to B |
| 3 | `retire_inactive_key = true` | A is destroyed |

**Overlap is the whole idea.** The window where a Worker restart cannot
authenticate is what this removes.

Two details that matter: retirement uses `count` so the key is **destroyed**, not
`disabled` — a disabled key is still a credential sitting in state. And step 3
should wait on *confirmed* deployment status, not an assumption, because it is the
step that immediately invalidates the old credential.

---

## 4. Split ownership — [`patterns/04-split-ownership`](patterns/04-split-ownership)

**Problem.** A single state file means every team that can change their own
Namespace can also change account-level roles, delete another team's Namespace,
and **read every API key token in state**. Terraform state is not access-controlled
per resource: whoever can apply can read all of it.

**Shape.** Two state files with different credentials.

- `platform/` owns Namespaces and account-level identities, and publishes
  Namespace IDs as outputs.
- `team-orders/` consumes those outputs through `terraform_remote_state` and owns
  only identities scoped to its own Namespace.

**The boundary is real**, not conventional: the team's credentials cannot modify
the platform state because they never have them.

The critical rule this enforces: **do not re-declare the Namespace in the team
config.** Two owners for one resource means Terraform fights itself on every
apply, each state seeing the other's changes as drift to correct.

---

## Rules that apply across all four

**One identity, one place.** Terraform writes the *complete* permission set on
every apply, so a user declared in two configurations has their access silently
overwritten by whichever applies last. No error, no warning.

**State holds secrets in plaintext.** `sensitive = true` redacts output; it is not
encryption. Any pattern here that creates an API key needs an encrypted remote
backend with restricted access.

**Terraform owns it or it does not.** A change made in the UI is drift, and the
next apply reverts it — including the emergency access someone granted at 2am.

**Guard the irreversible.** `prevent_destroy` on Namespaces, because deletion
destroys every Workflow history and `name` forces replacement, so a typo would
otherwise queue a destroy/create of production.

---

## Verification status

All five configurations pass `terraform init -backend=false` and
`terraform validate` against provider schema v1.7.0. **None has been applied** —
that needs a Temporal Cloud account. Validation proves the configuration is
well-formed and correctly typed; it does not prove the API accepts a given
combination at apply time. Run `terraform plan` against your own account first.
