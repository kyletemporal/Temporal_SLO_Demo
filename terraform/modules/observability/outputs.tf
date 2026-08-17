output "metrics_api_key" {
  description = "Bearer token for https://metrics.temporal.io/v1/metrics. Put it in a secret manager, not a scrape config in git."
  value       = temporalcloud_apikey.metrics.token
  sensitive   = true
}

output "service_account_id" {
  value = temporalcloud_service_account.metrics.id
}

output "openmetrics_endpoint" {
  description = "The current metrics endpoint. Scrapers only — opening it in a browser returns 'Jwt is missing'."
  value       = "https://metrics.temporal.io/v1/metrics"
}

output "prometheus_scrape_config" {
  description = "Drop-in scrape job. Render with `terraform output -raw prometheus_scrape_config`."
  sensitive   = true
  value       = <<-EOT
    # Add to prometheus.yml. Pairs with cloud/prometheus/ in this repo.
    - job_name: 'temporal-cloud'
      scrape_interval: 60s
      scrape_timeout: 30s
      honor_timestamps: true
      scheme: https
      metrics_path: '/v1/metrics'
      authorization:
        type: Bearer
        credentials: '${temporalcloud_apikey.metrics.token}'
      static_configs:
        - targets: ['metrics.temporal.io']
  EOT
}

output "legacy_promql_uri" {
  description = "URI of the DEPRECATED PromQL endpoint, if enabled. Disabled for everyone 2026-10-05."
  value       = try(temporalcloud_metrics_endpoint.legacy_promql[0].uri, null)
}
