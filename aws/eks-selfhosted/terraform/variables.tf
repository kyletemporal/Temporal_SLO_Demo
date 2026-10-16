variable "name" {
  description = "Name for the environment. Used as the EKS cluster name and as a prefix for everything else."
  type        = string
  default     = "temporal-selfhosted"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.name))
    error_message = "Lowercase letters, digits and hyphens, 3-31 characters, starting with a letter."
  }
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "tags" {
  description = "Extra tags applied to every resource."
  type        = map(string)
  default     = {}
}

# -----------------------------------------------------------------------------
# Network
# -----------------------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR for the new VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "single_nat_gateway" {
  description = <<-EOT
    One NAT gateway for the whole VPC instead of one per AZ. Cheaper (~$33/month
    instead of ~$100) and a single point of failure for egress — image pulls
    and AWS API calls from private subnets stop if its AZ goes down. Fine for a
    test environment; set false to rehearse an AZ failure honestly.
  EOT
  type        = bool
  default     = true
}

variable "admin_cidrs" {
  description = <<-EOT
    CIDRs allowed to reach the EKS public API endpoint (kubectl, helm). Required,
    with no default, because the only safe default is "nobody" and the only
    convenient one is 0.0.0.0/0. Use your office or VPN egress, e.g.
    ["203.0.113.10/32"].
  EOT
  type        = list(string)

  # Checks the PREFIX LENGTH, not the literal string. Rejecting only
  # "0.0.0.0/0" let ["0.0.0.0/1", "128.0.0.0/1"] through — the whole internet
  # in two entries. /16 is generous for an office or VPN egress range.
  validation {
    condition = length(var.admin_cidrs) > 0 && alltrue([
      for c in var.admin_cidrs : can(cidrnetmask(c)) && tonumber(split("/", c)[1]) >= 16
    ])
    error_message = "Set at least one CIDR, each /16 or narrower (e.g. 203.0.113.10/32). Wider ranges would open the cluster API to a large part of the internet."
  }
}

# -----------------------------------------------------------------------------
# EKS
# -----------------------------------------------------------------------------

variable "kubernetes_version" {
  description = "EKS Kubernetes version."
  type        = string
  default     = "1.36"
}

variable "node_instance_types" {
  description = <<-EOT
    Instance types for the managed node group. The default fits the Helm values
    in ../helm (3 history, 2 matching, 2 frontend, 2 worker, UI, admin-tools,
    and the monitoring stack) with room for application Workers.
  EOT
  type        = list(string)
  default     = ["m7i.xlarge"]
}

variable "node_count" {
  description = "Desired = min node count. Three spreads one node per AZ, which is what the topology spread constraints in the Helm values assume."
  type        = number
  default     = 3
}

variable "node_max_count" {
  description = "Upper bound for the node group."
  type        = number
  default     = 6
}

# -----------------------------------------------------------------------------
# RDS PostgreSQL
# -----------------------------------------------------------------------------

variable "postgres_engine_version" {
  description = <<-EOT
    PostgreSQL major version. 16 is the newest major Temporal lists in its
    tested dependency versions (13.18, 14.15, 15.10, 16.6) — see
    https://docs.temporal.io/temporal-service/persistence#dependency-versions.
    Newer majors probably work; "probably" is not the bar for a support repro.
  EOT
  type        = string
  default     = "16"
}

variable "db_instance_class" {
  description = "RDS instance class. History shard throughput is bounded by this more than by anything in Kubernetes."
  type        = string
  default     = "db.m7g.large"
}

variable "db_allocated_storage" {
  description = "Initial gp3 storage in GiB. Storage autoscaling grows it up to db_max_allocated_storage."
  type        = number
  default     = 100
}

variable "db_max_allocated_storage" {
  description = "Ceiling for RDS storage autoscaling, in GiB."
  type        = number
  default     = 500
}

variable "db_multi_az" {
  description = <<-EOT
    Multi-AZ standby. Off by default to halve the database cost of a test
    environment. Turn it on when the thing being reproduced involves failover —
    a standby promotion is a ~60-120s persistence outage that Temporal must ride
    through, and that is a different test from anything single-AZ can show.
  EOT
  type        = bool
  default     = false
}

variable "db_deletion_protection" {
  description = "Block `terraform destroy` from deleting the database. scripts/teardown.sh turns this off explicitly before destroying."
  type        = bool
  default     = true
}

variable "db_backup_retention_days" {
  description = "Automated backup retention."
  type        = number
  default     = 7
}
