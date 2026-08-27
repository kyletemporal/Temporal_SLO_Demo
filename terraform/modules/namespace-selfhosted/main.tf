# Namespaces on demand, for a SELF-HOSTED Temporal Service.
#
# ---------------------------------------------------------------------------
# READ THIS BEFORE USING IT
#
# There is NO Terraform provider for self-hosted Temporal namespaces. The
# temporalio/temporalcloud provider manages Temporal Cloud only — it talks to
# the Cloud Ops API, which a self-hosted cluster does not have. So this module
# drives the `temporal operator` CLI through provisioners.
#
# That is a real trade-off, not a hidden one:
#
#   WHAT YOU GET          declarative fleet definition, one map in one file,
#                         reviewable diffs, idempotent apply, and — unusually
#                         for a local-exec module — GENUINE DRIFT DETECTION,
#                         because `data.external` reads live namespace state on
#                         every plan and `check` blocks report the difference.
#
#   WHAT YOU DO NOT GET   a real resource graph. Terraform cannot roll back a
#                         half-finished CLI call, and `terraform plan` cannot
#                         show you a field-level diff the way a provider does.
#                         The check blocks tell you drift EXISTS; the apply is
#                         what reconciles it.
#
# If you are on Temporal Cloud, use ../namespace instead. It is a real provider
# resource and strictly better.
# ---------------------------------------------------------------------------

terraform {
  # 1.5+: `check` blocks (drift reporting) and `terraform_data` both land here.
  required_version = ">= 1.5"
  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}

locals {
  script = "${path.module}/scripts/nsctl.sh"

  # Flatten the per-namespace config once, applying defaults, so every resource
  # below reads the same normalised shape instead of repeating coalesce().
  ns = {
    for name, cfg in var.namespaces : name => {
      name          = name
      retention     = try(cfg.retention, null) != null ? cfg.retention : var.default_retention
      description   = try(cfg.description, "")
      owner_email   = try(cfg.owner_email, "")
      allow_destroy = try(cfg.allow_destroy, false)
      # `--data KEY=VALUE`, space separated for the shell loop in nsctl.sh.
      data = join(" ", [for k, v in try(cfg.data, {}) : "${k}=${v}"])
      # Search attributes as "Name=Type" pairs.
      attrs = join(" ", [for k, v in try(cfg.search_attributes, {}) : "${k}=${v}"])
    }
  }
}

# ---------------------------------------------------------------------------
# LIVE STATE
#
# Read on every plan. This is what turns a write-only local-exec module into one
# that can tell you the cluster no longer matches the file — someone creating a
# namespace by hand, or changing retention in the UI, shows up here.
#
# Returns exists="false" rather than failing when a namespace is absent, but
# FAILS LOUDLY if the cluster is unreachable: reporting a down cluster as "no
# namespaces exist" would make the next apply try to create all of them.
# ---------------------------------------------------------------------------
data "external" "current" {
  for_each = local.ns

  program = ["bash", local.script, "read"]
  query = {
    name    = each.key
    address = var.address
  }
}

# ---------------------------------------------------------------------------
# EXISTENCE
#
# triggers_replace is the NAME ONLY. Renaming a namespace is a destroy/create
# (there is no rename in Temporal), and that must be explicit and scary.
# Changing retention or description must NOT land here — see settings below.
# ---------------------------------------------------------------------------
resource "terraform_data" "namespace" {
  for_each = local.ns

  triggers_replace = each.key

  # Destroy provisioners can only reference `self`, never a variable, so
  # everything the delete needs is stashed in input at create time.
  input = {
    name          = each.value.name
    address       = var.address
    allow_destroy = each.value.allow_destroy
    script        = local.script
  }

  provisioner "local-exec" {
    command = "bash ${local.script} create"
    environment = {
      TF_NAME        = each.value.name
      TF_ADDRESS     = var.address
      TF_RETENTION   = each.value.retention
      TF_DESCRIPTION = each.value.description
      TF_EMAIL       = each.value.owner_email
      TF_DATA        = each.value.data
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "bash ${self.input.script} delete"
    environment = {
      TF_NAME             = self.input.name
      TF_ADDRESS          = self.input.address
      TF_ALLOW_DESTROY    = tostring(self.input.allow_destroy)
    }
  }
}

# ---------------------------------------------------------------------------
# MUTABLE SETTINGS
#
# A second resource keyed on the settings themselves. Changing retention
# replaces THIS resource — which re-runs its create provisioner, i.e. an update
# — while leaving the namespace itself alone. Without the split, editing
# retention would destroy and recreate the namespace, taking every Workflow
# history with it.
# ---------------------------------------------------------------------------
resource "terraform_data" "settings" {
  for_each = local.ns

  # The trigger watches BOTH the desired config AND the observed cluster state.
  #
  # Watching only the config was the first attempt and it was subtly broken:
  # when someone changed retention by hand, `check` reported the drift and
  # `apply` then did NOTHING, because from Terraform's point of view the config
  # had not changed and there was nothing to re-run. Detected but never fixed is
  # the worst of both worlds — it trains people to ignore the warning.
  #
  # Including the observed values means real-world drift changes the trigger,
  # which replaces this resource, which re-runs the update. Cost: because data
  # sources are read at PLAN time, reconciling out-of-band drift settles over
  # two applies — the first fixes the cluster, the second records the new
  # observation. Verified: 86400s -> 604800s, then clean.
  triggers_replace = {
    retention   = each.value.retention
    description = each.value.description
    email       = each.value.owner_email
    data        = each.value.data

    observed_retention   = data.external.current[each.key].result.retention_seconds
    observed_description = data.external.current[each.key].result.description
    observed_email       = data.external.current[each.key].result.owner_email
  }

  depends_on = [terraform_data.namespace]

  provisioner "local-exec" {
    command = "bash ${local.script} update"
    environment = {
      TF_NAME        = each.value.name
      TF_ADDRESS     = var.address
      TF_RETENTION   = each.value.retention
      TF_DESCRIPTION = each.value.description
      TF_EMAIL       = each.value.owner_email
      TF_DATA        = each.value.data
    }
  }
}

# ---------------------------------------------------------------------------
# SEARCH ATTRIBUTES
#
# ADDITIVE ONLY, and that is the server's rule, not this module's shortcut —
# the CLI's own help says to contact support to remove one. Deleting an entry
# from your tfvars therefore does nothing. See README.
# ---------------------------------------------------------------------------
resource "terraform_data" "search_attributes" {
  for_each = { for k, v in local.ns : k => v if v.attrs != "" }

  triggers_replace = each.value.attrs
  depends_on       = [terraform_data.namespace]

  provisioner "local-exec" {
    command = "bash ${local.script} attrs"
    environment = {
      TF_NAME    = each.value.name
      TF_ADDRESS = var.address
      TF_ATTRS   = each.value.attrs
    }
  }
}

# ---------------------------------------------------------------------------
# DRIFT REPORTING
#
# `check` blocks emit WARNINGS on plan and apply without failing the run, which
# is exactly right here: drift is information, and failing the plan would stop
# you running the apply that fixes it.
# ---------------------------------------------------------------------------
check "retention_matches" {
  assert {
    condition = alltrue([
      for name, cfg in local.ns :
      # Only compare where the namespace already exists — an absent one is a
      # pending create, not drift.
      data.external.current[name].result.exists != "true" ? true :
      tonumber(data.external.current[name].result.retention_seconds) == local.retention_seconds[name]
    ])
    error_message = join("\n", concat(
      ["Namespace retention on the cluster does not match this configuration:"],
      [for name, cfg in local.ns :
        format("  %s: cluster has %ss, config wants %ss (%s)",
          name,
          data.external.current[name].result.retention_seconds,
          local.retention_seconds[name],
          cfg.retention)
        if data.external.current[name].result.exists == "true" &&
      tonumber(data.external.current[name].result.retention_seconds) != local.retention_seconds[name]],
      ["", "Apply to reconcile. If the cluster value is the correct one, update the config instead."]
    ))
  }
}

check "namespaces_present" {
  assert {
    condition = alltrue([
      for name, cfg in local.ns :
      data.external.current[name].result.state == "" ||
      data.external.current[name].result.state == "NAMESPACE_STATE_REGISTERED"
    ])
    error_message = join("\n", concat(
      ["Namespaces exist but are not in the REGISTERED state:"],
      [for name, cfg in local.ns :
        format("  %s: %s", name, data.external.current[name].result.state)
        if data.external.current[name].result.state != "" &&
      data.external.current[name].result.state != "NAMESPACE_STATE_REGISTERED"],
      ["", "NAMESPACE_STATE_DELETED means a delete is in progress; it is not instant."]
    ))
  }
}

# Duration strings ("168h") cannot be compared against the seconds the API
# returns, so normalise here. Terraform has no duration parser, hence the
# arithmetic — h/m/d only, which covers every sane retention value.
locals {
  retention_seconds = {
    for name, cfg in local.ns : name => (
      endswith(cfg.retention, "h") ? tonumber(trimsuffix(cfg.retention, "h")) * 3600 :
      endswith(cfg.retention, "m") ? tonumber(trimsuffix(cfg.retention, "m")) * 60 :
      endswith(cfg.retention, "d") ? tonumber(trimsuffix(cfg.retention, "d")) * 86400 :
      tonumber(trimsuffix(cfg.retention, "s"))
    )
  }
}
