# THE SELF-SERVE CONTRACT.
#
# This block is the interface an application team fills in. Keep it small: every
# option added here is a decision delegated to a team that may not have the
# context for it, and a standard with fifty knobs is not a standard.
#
# Note what is deliberately NOT team-configurable — alert thresholds belong to
# the platform. A team that needs different numbers should be talking to the
# platform team, and requiring that conversation is the point.

variable "team_name" {
  description = "Team identifier. Appears on the folder, the alerts and the contact point."
  type        = string
}

variable "namespace" {
  description = "Temporal namespace this team's Workflows run in."
  type        = string
}

variable "task_queues" {
  description = "Task queues to scope dashboards and alerts to. Empty means the whole namespace."
  type        = list(string)
  default     = []
}

variable "sdk_emits_seconds" {
  description = <<-EOT
    True for Go and Java, false for TypeScript, Python and .NET.

    Not a style choice. Go/Java emit latency histograms in SECONDS with a
    _seconds suffix; the others emit MILLISECONDS with no suffix. Both the metric
    NAME and the threshold arithmetic change. Getting it wrong yields an alert
    that either never fires or never stops, with no error to say why.

    Verify against your own Workers: demo/scripts/verify-sdk-labels.sh
  EOT
  type        = bool
  default     = true
}

variable "prometheus_datasource_uid" {
  description = "UID of the Grafana datasource pointing at the platform's Prometheus."
  type        = string
}

variable "loki_datasource_uid" {
  description = "UID of the Loki datasource. Logs are the only place a workflow_id may live."
  type        = string
  default     = "loki"
}

variable "grafana_team_ids" {
  description = <<-EOT
    Grafana team IDs granted Edit on this folder.

    Folder permissions are managed as a COMPLETE SET — anything not declared is
    removed — so this list plus the Viewer role is the whole access story for
    the folder.

    LEAVING THIS EMPTY SKIPS THE PERMISSION RESOURCE ENTIRELY: the folder then
    inherits Grafana's default permissions rather than being locked down. That is
    the safe default for a first apply (it cannot lock you out), but it is NOT
    the end state — pass the team's ID once you have it.
  EOT
  type        = list(string)
  default     = []
}

variable "slack_webhook_url" {
  description = "Slack webhook for this team's alerts. Pass from a secret store, never a literal."
  type        = string
  default     = null
  sensitive   = true
}

variable "email_addresses" {
  description = <<-EOT
    Email recipients.

    At least one contact method — this or slack_webhook_url — is required, and
    enforced by a precondition on the contact point. With neither, the contact
    point has no integrations, applies cleanly, and pages nobody.
  EOT
  type        = list(string)
  default     = []
}

variable "runbook_url" {
  description = "Runbook link attached to every alert. An alert without one is a page someone improvises against."
  type        = string
  default     = ""
}

variable "enable_nondeterminism_alert" {
  description = <<-EOT
    Enable the non-determinism alert.

    Default true because an NDE is unrecoverable without a code change. Turn it
    off only if your SDK reports the failure under a different label — the rule
    selects on failure_reason="NonDeterminismError", verified on Go via tally and
    possibly different elsewhere. A wrong label yields a silent alert, which is
    worse than no alert at all.
  EOT
  type        = bool
  default     = true
}

# --- Platform-owned thresholds. Teams do not set these. ---

variable "schedule_to_start_p99_threshold_ms" {
  description = "Platform standard. Temporal's published guidance is 200ms."
  type        = number
  default     = 200
}

variable "workflow_failure_ratio_threshold" {
  description = "Platform standard. A FRACTION of Workflows failing, not a count."
  type        = number
  default     = 0.10

  validation {
    condition     = var.workflow_failure_ratio_threshold > 0 && var.workflow_failure_ratio_threshold < 1
    error_message = "workflow_failure_ratio_threshold is a ratio between 0 and 1, e.g. 0.10 for 10%."
  }
}
