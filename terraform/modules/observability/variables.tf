variable "service_account_name" {
  description = "Name for the metrics-read service account."
  type        = string
  default     = "metrics-scraper"
}

variable "apikey_expiry_time" {
  description = <<-EOT
    RFC3339 expiry for the scrape credential.

    When this expires your metrics silently stop arriving, and a metrics pipeline
    that stops is exactly the failure that looks like "everything is fine" on a
    dashboard. Alert on scrape staleness as well as setting a rotation reminder —
    the same reasoning as the monitor's poll-freshness alert.
  EOT
  type        = string

  validation {
    condition     = can(formatdate("YYYY-MM-DD", var.apikey_expiry_time))
    error_message = "apikey_expiry_time must be RFC3339, e.g. 2027-01-01T00:00:00Z."
  }
}

variable "enable_deprecated_promql_endpoint" {
  description = <<-EOT
    Provision the DEPRECATED mTLS PromQL endpoint (temporal_cloud_v0_* metrics).

    Disabled 2026-10-05 for all users and closed to new ones. Enable only to
    manage an existing dependency while migrating to OpenMetrics.
  EOT
  type        = bool
  default     = false
}

variable "promql_accepted_client_ca" {
  description = "Base64-encoded PEM CA cert for the deprecated PromQL endpoint."
  type        = string
  default     = null
}
