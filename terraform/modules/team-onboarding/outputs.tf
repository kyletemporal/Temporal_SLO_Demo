output "namespace_id" {
  value = module.namespace.id
}

output "namespace_endpoints" {
  value = module.namespace.endpoints
}

output "service_account_id" {
  value = temporalcloud_service_account.workload.id
}

# Marked sensitive so it is redacted from plan/apply output and CI logs.
#
# Sensitive is NOT encryption: the value is still plaintext in state. Read it
# deliberately with `terraform output -raw workload_api_key` and put it straight
# into your secret manager.
output "workload_api_key" {
  description = "API key token for the team's Worker identity. Store in a secret manager immediately."
  value       = temporalcloud_apikey.workload.token
  sensitive   = true
}
