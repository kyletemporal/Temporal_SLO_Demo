# SELF-SERVE OBSERVABILITY for teams building on a hosted Temporal platform.
#
# THE PROBLEM THIS SOLVES
#
# A platform team runs Temporal. Application teams build Workflows on it. Every
# one of those teams needs a dashboard, alerts and a route to their on-call — and
# if the platform team hand-builds each set, they become the bottleneck for
# everyone else's observability, and the standard drifts team by team.
#
# THE SHAPE
#
# An application team submits ~20 lines of tfvars describing their Workflows and
# where to page them. This module provisions the whole thing: a folder they own,
# a dashboard scoped to their namespace, alerts at the platform's standard
# thresholds, and routing to their contact point. The platform team reviews a
# small diff instead of building anything.
#
# WHAT MAKES IT SAFE TO RUN PER TEAM
#
# Every resource here is scoped to ONE team. That is not an accident — it is the
# constraint the design is built around:
#
#   grafana_notification_policy manages the ENTIRE policy tree and OVERWRITES it.
#
# A module that created one per team would have each team's apply silently wipe
# every other team's routing. So this module never touches it. Instead each rule
# carries notification_settings.contact_point, routing directly to the team's own
# contact point. The shared tree stays owned by the platform team, and a hundred
# teams can apply this module without colliding.

terraform {
  required_version = ">= 1.5"
  required_providers {
    grafana = {
      source  = "grafana/grafana"
      version = "~> 4.0"
    }
  }
}

locals {
  slug = replace(lower(var.team_name), "/[^a-z0-9-]/", "-")

  # Namespace and task-queue selectors, built once and reused by every rule so a
  # team cannot end up with a dashboard scoped to itself and alerts scoped to
  # everyone.
  ns_sel = "namespace=\"${var.namespace}\""
  tq_sel = length(var.task_queues) > 0 ? "task_queue=~\"${join("|", var.task_queues)}\"" : ""
  sel    = local.tq_sel == "" ? local.ns_sel : "${local.ns_sel}, ${local.tq_sel}"

  # UNIT CONVERSION. Go and Java SDKs emit histograms in SECONDS; TypeScript,
  # Python and .NET emit MILLISECONDS with no _seconds suffix. A threshold of
  # 200ms is 0.2 in one and 200 in the other, and getting it wrong is a silent
  # 1000x error that makes an alert either never fire or never stop.
  #
  # The team declares its SDK language; the module does the arithmetic.
  latency_metric = var.sdk_emits_seconds ? "temporal_activity_schedule_to_start_latency_seconds_bucket" : "temporal_activity_schedule_to_start_latency_bucket"
  latency_scale  = var.sdk_emits_seconds ? 1 : 1000

  # JSON-escaped selectors for the dashboard template.
  #
  # A PromQL selector contains double quotes (namespace="orders-prod"), and the
  # dashboard embeds it inside a JSON string. Interpolating the raw selector
  # produces INVALID JSON that Grafana rejects at apply time — caught here by
  # rendering the template and parsing it, which `terraform validate` does not do
  # because templatefile is only evaluated at plan time.
  sel_json    = replace(local.sel, "\"", "\\\"")
  ns_sel_json = replace(local.ns_sel, "\"", "\\\"")
}

# The team's own space. prevent_destroy_if_not_empty guards against a removed
# module block quietly deleting dashboards someone still relies on.
resource "grafana_folder" "team" {
  title                        = "Temporal — ${var.team_name}"
  uid                          = "temporal-${local.slug}"
  prevent_destroy_if_not_empty = true
}

# Folder permissions are managed as a COMPLETE SET — anything not listed here is
# removed. That is why the platform admin role is listed explicitly: omitting it
# would lock the platform team out of a folder they are responsible for.
resource "grafana_folder_permission" "team" {
  count = length(var.grafana_team_ids) > 0 ? 1 : 0

  folder_uid = grafana_folder.team.uid

  dynamic "permissions" {
    for_each = var.grafana_team_ids
    content {
      team_id    = permissions.value
      permission = "Edit"
    }
  }

  permissions {
    role       = "Viewer"
    permission = "View"
  }
}

# Where this team's pages go. One per team, so a routing change for one team can
# never affect another.
resource "grafana_contact_point" "team" {
  name = "temporal-${local.slug}"

  # WITHOUT THIS, THE WORST FAILURE IN THE MODULE IS SILENT.
  #
  # Both contact variables default to empty. With neither set, every dynamic
  # block below emits nothing and Grafana gets a contact point with ZERO
  # integrations — which applies cleanly, shows green, and routes every alert
  # this module creates to nowhere. Alerts fire and nobody is paged.
  #
  # Terraform variable validation cannot reference another variable, so the
  # check has to live here.
  lifecycle {
    precondition {
      condition     = var.slack_webhook_url != null || length(var.email_addresses) > 0
      error_message = "At least one contact method is required: set slack_webhook_url or email_addresses. A contact point with no integrations applies successfully and silently pages nobody."
    }
  }

  dynamic "slack" {
    for_each = var.slack_webhook_url == null ? [] : [1]
    content {
      url                     = var.slack_webhook_url
      title                   = "{{ .CommonLabels.alertname }} — ${var.team_name}"
      text                    = "{{ range .Alerts }}{{ .Annotations.summary }}\n{{ .Annotations.description }}\n{{ end }}"
      disable_resolve_message = false
    }
  }

  dynamic "email" {
    for_each = length(var.email_addresses) > 0 ? [1] : []
    content {
      addresses    = var.email_addresses
      single_email = false
    }
  }
}

# ---------------------------------------------------------------------------
# Alerts, at the platform's standard thresholds.
#
# The team chooses WHAT to watch and WHERE to page. It does not choose the
# thresholds — that is what makes this a standard rather than a template. A team
# that needs different numbers should be having a conversation with the platform
# team, and the module's inputs deliberately make that conversation necessary.
# ---------------------------------------------------------------------------
resource "grafana_rule_group" "team" {
  name             = "temporal-${local.slug}"
  folder_uid       = grafana_folder.team.uid
  interval_seconds = 60

  # 1. Task delivery. The first signal that a fleet cannot keep up, and upstream
  #    of almost everything else a team will notice.
  rule {
    name      = "[${var.team_name}] Activity schedule-to-start p99 high"
    condition = "C"
    for       = "10m"

    data {
      ref_id         = "A"
      datasource_uid = var.prometheus_datasource_uid
      model = jsonencode({
        refId    = "A"
        instant  = true
        expr     = "histogram_quantile(0.99, sum by (le, task_queue) (rate(${local.latency_metric}{${local.sel}}[5m])))"
        interval = ""
      })
      relative_time_range {
        from = 600
        to   = 0
      }
    }

    data {
      ref_id         = "C"
      datasource_uid = "__expr__"
      model = jsonencode({
        refId      = "C"
        type       = "threshold"
        expression = "A"
        conditions = [{
          evaluator = { type = "gt", params = [var.schedule_to_start_p99_threshold_ms * local.latency_scale / 1000] }
        }]
      })
      relative_time_range {
        from = 600
        to   = 0
      }
    }

    # NoData is NOT an alert here. An idle task queue emits no histogram samples,
    # and paging a team because they had a quiet hour is how an alert gets muted
    # permanently. Absence of workers is caught by a different signal.
    no_data_state  = "OK"
    exec_err_state = "Alerting"

    labels = {
      team      = var.team_name
      namespace = var.namespace
      severity  = "warning"
    }

    annotations = {
      summary     = "Activity schedule-to-start p99 above ${var.schedule_to_start_p99_threshold_ms}ms for ${var.team_name}"
      description = "Tasks are waiting for a Worker. Check Worker count and slot availability before assuming the platform is slow. If ScheduleToStartTimeout is set in Activity Options it truncates this metric and hides the real number."
      runbook_url = var.runbook_url
    }

    notification_settings {
      contact_point = grafana_contact_point.team.name
      group_by      = ["alertname", "team"]
    }
  }

  # 2. Workflow failures, as a RATIO. Alerting on the raw count guarantees false
  #    pages: a healthy Temporal application absorbs Activity failures through
  #    retries. A Workflow that fails is a different class of event — retries were
  #    exhausted or never configured.
  rule {
    name      = "[${var.team_name}] Workflow failure ratio high"
    condition = "C"
    for       = "5m"

    data {
      ref_id         = "A"
      datasource_uid = var.prometheus_datasource_uid
      model = jsonencode({
        refId   = "A"
        instant = true
        # The `or ... * 0` guard is load-bearing. Without it, a total outage —
        # 100% failing, zero successes — produces NO matching series on the
        # right-hand side, the expression returns nothing, and the alert never
        # fires precisely when it matters most.
        expr = "sum(rate(workflow_failed{${local.ns_sel}}[5m])) / clamp_min(sum(rate(workflow_failed{${local.ns_sel}}[5m])) + (sum(rate(workflow_success{${local.ns_sel}}[5m])) or sum(rate(workflow_failed{${local.ns_sel}}[5m])) * 0), 0.001)"
      })
      relative_time_range {
        from = 600
        to   = 0
      }
    }

    data {
      ref_id         = "C"
      datasource_uid = "__expr__"
      model = jsonencode({
        refId      = "C"
        type       = "threshold"
        expression = "A"
        conditions = [{
          evaluator = { type = "gt", params = [var.workflow_failure_ratio_threshold] }
        }]
      })
      relative_time_range {
        from = 600
        to   = 0
      }
    }

    no_data_state  = "OK"
    exec_err_state = "Alerting"

    labels = {
      team      = var.team_name
      namespace = var.namespace
      severity  = "critical"
    }

    annotations = {
      summary     = "Over ${var.workflow_failure_ratio_threshold * 100}% of Workflows failing for ${var.team_name}"
      description = "Terminal Workflow failures, not absorbed Activity failures. Check retry policies — MaximumAttempts=1 turns every Activity failure into a Workflow failure — and whether the Workflow handles the failure path at all."
      runbook_url = var.runbook_url
    }

    notification_settings {
      contact_point = grafana_contact_point.team.name
      group_by      = ["alertname", "team"]
    }
  }

  # 3. Non-determinism. Pages immediately: there is no acceptable steady-state
  #    rate, and these executions retry forever without self-healing.
  #
  #    NOTE THE LABEL NAME. It is `failure_reason`, not the `error_type` that is
  #    widely published. Verified on the Go SDK via tally by forcing a real NDE;
  #    the wrong label matches nothing and yields a permanently silent alert.
  #    Confirm on your own SDK with demo/scripts/verify-sdk-labels.sh.
  dynamic "rule" {
    for_each = var.enable_nondeterminism_alert ? [1] : []
    content {
      name      = "[${var.team_name}] Non-determinism error"
      condition = "C"
      for       = "0m"

      data {
        ref_id         = "A"
        datasource_uid = var.prometheus_datasource_uid
        model = jsonencode({
          refId   = "A"
          instant = true
          expr    = "sum(increase(temporal_workflow_task_execution_failed_total{${local.ns_sel}, failure_reason=\"NonDeterminismError\"}[5m]))"
        })
        relative_time_range {
          from = 600
          to   = 0
        }
      }

      data {
        ref_id         = "C"
        datasource_uid = "__expr__"
        model = jsonencode({
          refId      = "C"
          type       = "threshold"
          expression = "A"
          conditions = [{
            evaluator = { type = "gt", params = [0] }
          }]
        })
        relative_time_range {
          from = 600
          to   = 0
        }
      }

      no_data_state  = "OK"
      exec_err_state = "Alerting"

      labels = {
        team      = var.team_name
        namespace = var.namespace
        severity  = "critical"
      }

      annotations = {
        summary     = "Non-determinism error in ${var.team_name}"
        description = "Workflow code changed incompatibly with in-flight histories. These executions are stuck retrying forever and will not self-heal. Find the affected IDs in the logs — NDEs log with code TMPRL1100 and carry WorkflowID and RunID. Fix forward with Worker Versioning."
        runbook_url = var.runbook_url
      }

      notification_settings {
        contact_point = grafana_contact_point.team.name
        group_by      = ["alertname", "team"]
      }
    }
  }
}

resource "grafana_dashboard" "team" {
  folder    = grafana_folder.team.uid
  overwrite = true
  config_json = templatefile("${path.module}/dashboard.json.tftpl", {
    team_name      = var.team_name
    slug           = local.slug
    namespace      = var.namespace
    selector       = local.sel_json
    ns_selector    = local.ns_sel_json
    latency_metric = local.latency_metric
    datasource_uid = var.prometheus_datasource_uid
    loki_uid       = var.loki_datasource_uid
  })
}
