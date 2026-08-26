variable "address" {
  description = <<-EOT
    Temporal Frontend gRPC address, e.g. localhost:7233.

    NOTE for the Compose demo: auto-setup binds the Frontend to the container's
    own eth0 address, so from INSIDE the container 127.0.0.1:7233 is refused.
    From the host, localhost:7233 works because the port is published.
  EOT
  type        = string
  default     = "localhost:7233"
}

variable "default_retention" {
  description = <<-EOT
    Retention applied to any namespace that does not set its own.

    The SERVER default is 72h. This module defaults to 168h (7 days) instead,
    deliberately: 72h means a Friday incident is uninvestigable by Monday, and
    raising retention later does NOT bring back what has already been deleted.
  EOT
  type        = string
  default     = "168h"

  validation {
    condition     = can(regex("^[0-9]+[smhd]$", var.default_retention))
    error_message = "default_retention must be a duration like 72h, 168h or 30d."
  }
}

variable "namespaces" {
  description = <<-EOT
    The namespace fleet. Map key is the namespace name.

      namespaces = {
        payments = {
          retention   = "720h"
          description = "Payments team"
          owner_email = "payments@example.com"
          data        = { team = "payments", tier = "1" }
          search_attributes = {
            OrderPriority = "Keyword"
            CustomerTier  = "Keyword"
          }
        }
        orders = {}          # inherits default_retention
      }

    SEARCH ATTRIBUTES ARE ADDITIVE ONLY. The server has no delete for them
    (the CLI tells you to contact support), so removing one from this map is
    silently a no-op rather than a removal.

    allow_destroy defaults to FALSE. A `terraform destroy` against a namespace
    without it FAILS rather than deleting, because deleting a namespace deletes
    every Workflow history in it with no undo. Flip it in its own reviewed
    commit, apply, then destroy.
  EOT

  type = map(object({
    retention         = optional(string)
    description       = optional(string, "")
    owner_email       = optional(string, "")
    allow_destroy     = optional(bool, false)
    data              = optional(map(string), {})
    search_attributes = optional(map(string), {})
  }))
  default = {}

  validation {
    condition = alltrue([
      for n, c in var.namespaces :
      c.retention == null || can(regex("^[0-9]+[smhd]$", c.retention))
    ])
    error_message = "Each retention must be a duration like 72h, 168h or 30d."
  }

  validation {
    # Temporal accepts a wide range of names, but anything outside this set
    # makes CLI quoting and Prometheus label matching fragile. Constrain early:
    # a namespace cannot be renamed, so a bad name is permanent.
    condition = alltrue([
      for n, c in var.namespaces : can(regex("^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$", n))
    ])
    error_message = "Namespace names must be 1-63 chars of [A-Za-z0-9._-] and start alphanumeric."
  }

  validation {
    condition = alltrue([
      for n, c in var.namespaces : alltrue([
        for k, t in c.search_attributes :
        contains(["Text", "Keyword", "Int", "Double", "Bool", "Datetime", "KeywordList"], t)
      ])
    ])
    error_message = "Search attribute types must be one of: Text, Keyword, Int, Double, Bool, Datetime, KeywordList."
  }
}
