variable "grafana_url" {
  type = string
}

variable "grafana_auth" {
  description = "Grafana service account token. From a secret store, never a literal."
  type        = string
  sensitive   = true
}

variable "payments_grafana_team_id" {
  type = string
}

variable "payments_slack_webhook" {
  type      = string
  sensitive = true
}
