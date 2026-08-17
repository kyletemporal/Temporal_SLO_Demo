# Metrics access for the cloud/ bundle in this repo.

terraform {
  required_version = ">= 1.5"
  required_providers {
    temporalcloud = {
      source  = "temporalio/temporalcloud"
      version = "~> 1.7"
    }
  }
}

provider "temporalcloud" {}

module "metrics" {
  source = "../../modules/observability"

  service_account_name = "prometheus-scraper"
  apikey_expiry_time   = "2027-01-01T00:00:00Z"

  # Deliberately NOT enabling the deprecated PromQL endpoint. See the module
  # header: it is disabled for everyone on 2026-10-05.
  enable_deprecated_promql_endpoint = false
}

# Renders a ready-to-paste Prometheus job:
#   terraform output -raw prometheus_scrape_config >> prometheus.yml
output "prometheus_scrape_config" {
  value     = module.metrics.prometheus_scrape_config
  sensitive = true
}
