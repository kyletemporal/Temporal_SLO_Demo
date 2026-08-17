variable "team_name" {
  description = "Team identifier. Used as the namespace name and as a prefix for identities."
  type        = string
}

variable "regions" {
  type = list(string)
}

variable "retention_days" {
  type    = number
  default = 30
}

variable "search_attributes" {
  description = "Custom search attributes for this team's namespace."
  type        = map(string)
  default     = {}
}

variable "apikey_expiry_time" {
  description = <<-EOT
    RFC3339 expiry for the workload API key. There is no 'never expires' option
    and that is deliberate: an expiry you have to renew is a rotation you cannot
    forget. Set a calendar reminder before this date — the key stops working on
    it, and Workers will fail to connect.
  EOT
  type        = string

  validation {
    condition     = can(formatdate("YYYY-MM-DD", var.apikey_expiry_time))
    error_message = "apikey_expiry_time must be an RFC3339 timestamp, e.g. 2027-01-01T00:00:00Z."
  }
}

variable "team_members" {
  description = <<-EOT
    Human users, keyed by email:
      { "dev@example.com" = { account_access = "Developer", namespace_permission = "Write" } }

    account_access is the ACCOUNT-level role (Admin, Developer, Read, FinanceAdmin).
    Admins implicitly get every namespace, so namespace_permission is ignored for them.
  EOT
  type = map(object({
    account_access       = string
    namespace_permission = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for m in values(var.team_members) :
      contains(["Admin", "Developer", "Read", "FinanceAdmin"], m.account_access)
    ])
    error_message = "account_access must be one of Admin, Developer, Read, FinanceAdmin."
  }

  validation {
    condition = alltrue([
      for m in values(var.team_members) :
      contains(["Admin", "Write", "Read"], m.namespace_permission)
    ])
    error_message = "namespace_permission must be one of Admin, Write, Read."
  }
}

variable "create_timeout" {
  type    = string
  default = "15m"
}
