variable "namespace_id" {
  description = "Fully qualified Temporal Cloud namespace, e.g. orders-prod.a1b2c3."
  type        = string
}

variable "namespace_name" {
  description = "Short namespace name, used for bucket and role naming."
  type        = string
}

variable "namespace_region" {
  description = <<-EOT
    AWS region of the Temporal Cloud NAMESPACE, e.g. us-east-1.

    Compared against var.region by a precondition. Temporal requires the bucket
    to live in the same region as the namespace, and a mismatch produces a sink
    that provisions cleanly and silently never delivers.
  EOT
  type        = string
}

variable "region" {
  description = "AWS region for the S3 bucket. Must equal namespace_region."
  type        = string
}

variable "temporal_cloud_principal_arns" {
  description = <<-EOT
    IAM principals Temporal Cloud assumes this role from.

    NOT DEFAULTED, AND THAT IS DELIBERATE. Temporal Cloud writes using multiple
    intermediary IAM roles chosen at random — for security isolation, load
    distribution and failover — and that set is account/region specific and can
    be rotated by Temporal.

    A hardcoded guess would produce a trust policy that works right up until it
    silently does not, which is the worst possible failure for an audit trail.

    Get the real values from the CloudFormation template the Cloud UI generates:
      Namespace -> Export -> Configure -> Access method: Manual -> Template URL

    Then verify before trusting it:
      temporal cloud namespace export s3 validate \
        --namespace <ns.acct> --sink-name <name> \
        --role-arn <role-arn> --bucket-name <bucket> --region <region>
  EOT
  type        = list(string)

  validation {
    condition     = length(var.temporal_cloud_principal_arns) > 0
    error_message = "At least one Temporal Cloud principal ARN is required. Take it from the CloudFormation template in the Cloud UI — do not guess."
  }
}

variable "external_id" {
  description = <<-EOT
    sts:ExternalId for confused-deputy protection.

    Strongly recommended. Without it, anyone who learns your role ARN and can
    persuade Temporal to assume roles could reach your bucket. Use the value
    Temporal's CloudFormation template specifies.
  EOT
  type        = string
  default     = null
}

variable "sink_name" {
  description = "Export sink name. Cannot be changed after creation."
  type        = string
  default     = "history-export"
}

variable "bucket_name" {
  description = "Bucket name. Defaults to temporal-history-<namespace>-<account-id>."
  type        = string
  default     = null
}

variable "kms_key_arn" {
  description = <<-EOT
    KMS key for server-side encryption. Optional.

    If you add this LATER, the IAM role must be updated too — Temporal's docs
    call this out specifically. This module handles it, but only if you re-apply;
    changing the key outside Terraform breaks export at write time.
  EOT
  type        = string
  default     = null
}

variable "enabled" {
  description = "Whether the sink is active."
  type        = bool
  default     = true
}

variable "transition_to_ia_days" {
  description = "Days before moving exported history to STANDARD_IA."
  type        = number
  default     = 90
}

variable "transition_to_glacier_days" {
  description = "Days before moving to GLACIER. Retrieval takes hours — fine for audit, not for debugging."
  type        = number
  default     = 365
}

variable "tags" {
  type    = map(string)
  default = {}
}
