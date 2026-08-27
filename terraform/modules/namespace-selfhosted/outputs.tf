output "namespaces" {
  description = "Namespace name -> live state as read from the cluster during the last plan."
  value = {
    for name, d in data.external.current : name => {
      exists            = d.result.exists == "true"
      id                = d.result.id
      state             = d.result.state
      retention_seconds = d.result.retention_seconds
      description       = d.result.description
      owner_email       = d.result.owner_email
      # "Name=Type,Name=Type" — the CUSTOM attributes only. System attributes
      # are always present and are not managed here.
      custom_search_attributes = d.result.custom_attributes
    }
  }
}

output "names" {
  description = "Namespace names managed by this module."
  value       = sort(keys(var.namespaces))
}

# Wiring for the rest of the repo: monitor/slo-config.yaml and the Prometheus
# rules both need the namespace list, and hand-copying it is how they drift.
output "monitor_namespaces" {
  description = "Namespace names formatted for monitor/slo-config.yaml."
  value       = [for n in sort(keys(var.namespaces)) : n]
}

output "prometheus_namespace_regex" {
  description = <<-EOT
    Anchored regex matching exactly these namespaces, for Prometheus rules:
      namespace=~"<this>"
    Regenerate your rules when the fleet changes rather than maintaining the
    pattern by hand.
  EOT
  value       = length(var.namespaces) == 0 ? "" : "^(${join("|", sort(keys(var.namespaces)))})$"
}
