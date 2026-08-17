variable "name" {
  description = "Namespace name. Changing this REPLACES the namespace and destroys all Workflow history."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$", var.name))
    error_message = "Namespace names are lowercase alphanumeric with hyphens, 3-40 characters."
  }
}

variable "regions" {
  description = "Regions, e.g. [\"aws-us-east-1\"]. CANNOT be changed after creation."
  type        = list(string)

  validation {
    condition     = length(var.regions) > 0
    error_message = "At least one region is required."
  }
}

variable "retention_days" {
  description = "Workflow history retention. Also the ceiling on any Visibility-based SLO window."
  type        = number
  default     = 30

  validation {
    # The floor is a Temporal limit; the ceiling here is ours, to stop a typo
    # turning into a large bill silently.
    condition     = var.retention_days >= 1 && var.retention_days <= 90
    error_message = "retention_days must be between 1 and 90. Above 90 needs a deliberate override and a cost conversation."
  }
}

variable "api_key_auth" {
  description = "Use API key authentication. Mutually exclusive with accepted_client_ca."
  type        = bool
  default     = true
}

variable "accepted_client_ca" {
  description = "Base64-encoded CA cert (PEM) for mTLS. Leave null when api_key_auth is true."
  type        = string
  default     = null
}

variable "certificate_filters" {
  description = "Optional mTLS certificate filters. Only meaningful with accepted_client_ca."
  type = list(object({
    common_name              = optional(string)
    organization             = optional(string)
    organizational_unit      = optional(string)
    subject_alternative_name = optional(string)
  }))
  default = null
}

variable "codec_server" {
  description = "Optional codec server, so the UI can decode encrypted payloads."
  type = object({
    endpoint                         = string
    pass_access_token                = optional(bool)
    include_cross_origin_credentials = optional(bool)
    custom_error_message             = optional(string)
    custom_error_link                = optional(string)
  })
  default = null
}

variable "search_attributes" {
  description = "Custom search attributes as {name = type}. Types: Bool, Datetime, Double, Int, Keyword, KeywordList, Text."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for t in values(var.search_attributes) :
      contains(["Bool", "Datetime", "Double", "Int", "Keyword", "KeywordList", "Text"], t)
    ])
    error_message = "Search attribute types must be one of Bool, Datetime, Double, Int, Keyword, KeywordList, Text."
  }
}

variable "create_timeout" {
  description = "Namespace creation can take minutes, especially multi-region."
  type        = string
  default     = "15m"
}

variable "delete_timeout" {
  type    = string
  default = "15m"
}
