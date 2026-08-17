output "id" {
  description = "Namespace ID, in the form <name>.<account-id>. Use this for grants and Nexus endpoints."
  value       = temporalcloud_namespace.this.id
}

output "name" {
  value = temporalcloud_namespace.this.name
}

output "endpoints" {
  description = "gRPC and web endpoints for this Namespace."
  value       = temporalcloud_namespace.this.endpoints
}
