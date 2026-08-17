# What an application team actually submits.
#
# This is the whole self-serve interface: a team opens a PR containing this
# block, the platform team reviews a small diff, and the team gets a folder,
# dashboard, alerts and paging at the platform's standard thresholds.

terraform {
  required_version = ">= 1.5"
  required_providers {
    grafana = {
      source  = "grafana/grafana"
      version = "~> 4.0"
    }
  }
}

provider "grafana" {
  url  = var.grafana_url
  auth = var.grafana_auth
}

module "payments_observability" {
  source = "../../modules/self-serve-observability"

  team_name   = "payments"
  namespace   = "payments-prod"
  task_queues = ["payments-main", "payments-refunds"]

  # Go SDK. Set false for TypeScript, Python or .NET — it changes the metric
  # NAME and the threshold arithmetic, and getting it wrong is a silent 1000x
  # error. Confirm with: cd demo && make verify-sdk-labels
  sdk_emits_seconds = true

  prometheus_datasource_uid = "prometheus"
  loki_datasource_uid       = "loki"

  grafana_team_ids = [var.payments_grafana_team_id]

  slack_webhook_url = var.payments_slack_webhook
  runbook_url       = "https://runbooks.example.com/temporal/payments"
}

output "folder_url" {
  value = module.payments_observability.folder_url
}

# The platform team's notification policy can reference this. The module does
# not create a policy itself — that resource overwrites the entire tree.
output "contact_point" {
  value = module.payments_observability.contact_point_name
}
