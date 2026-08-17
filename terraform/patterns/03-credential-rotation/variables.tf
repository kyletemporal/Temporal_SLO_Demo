variable "namespace_id" {
  description = "Namespace this workload identity can write to."
  type        = string
}

variable "active_key_slot" {
  description = "Which slot Workers are currently using: a or b."
  type        = string
  default     = "a"
  validation {
    condition     = contains(["a", "b"], var.active_key_slot)
    error_message = "active_key_slot must be 'a' or 'b'."
  }
}

variable "retire_inactive_key" {
  description = <<-EOT
    Destroy the INACTIVE slot. Leave false until every Worker is confirmed to be
    using the active slot — checking deployment status, not assuming it. This is
    the step that is irreversible in the sense that matters: the old credential
    stops working immediately.
  EOT
  type        = bool
  default     = false
}

variable "slot_a_expiry" {
  type = string
}

variable "slot_b_expiry" {
  type = string
}
