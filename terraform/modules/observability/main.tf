# Provision metrics access for Temporal Cloud.
#
# READ THIS BEFORE CHOOSING: THERE ARE TWO METRICS ENDPOINTS AND THEY ARE NOT
# INTERCHANGEABLE.
#
#   1. OpenMetrics — CURRENT.  https://metrics.temporal.io/v1/metrics
#      Bearer API key. Metrics are named temporal_cloud_v1_*.
#      Provisioned here by a `metricsread` service account plus an API key.
#
#   2. PromQL      — DEPRECATED. Per-account URI, mTLS client certificates.
#      Metrics are named temporal_cloud_v0_*.
#      Deprecated 2026-04-02, CLOSED TO NEW USERS, and DISABLED FOR EVERYONE ON
#      2026-10-05.
#
# The provider's `temporalcloud_metrics_endpoint` resource — the one whose name
# suggests it is the obvious choice — provisions endpoint (2), the deprecated
# one. Its schema gives it away: it takes `accepted_client_ca` and returns a
# "Prometheus metrics endpoint URI". Reaching for it on a new build wires you to
# something that stops working, so it is OFF by default here and gated behind a
# variable that says so.
#
# This module defaults to the current path.

terraform {
  required_version = ">= 1.5"
  required_providers {
    temporalcloud = {
      source  = "temporalio/temporalcloud"
      version = "~> 1.7"
    }
  }
}

# Least privilege, and the provider supports it exactly.
#
# `metricsread` is an account-level role that grants metrics reads and nothing
# else. Scrapers get scraped-shaped access: an `admin` key for a Grafana scrape
# job is a standing account-takeover credential living in a config file.
resource "temporalcloud_service_account" "metrics" {
  name           = var.service_account_name
  description    = "Read-only metrics scraper (OpenMetrics endpoint). Managed by Terraform."
  account_access = "metricsread"
}

resource "temporalcloud_apikey" "metrics" {
  display_name = var.service_account_name
  description  = "OpenMetrics scrape credential. Managed by Terraform."
  owner_type   = "service-account"
  owner_id     = temporalcloud_service_account.metrics.id
  expiry_time  = var.apikey_expiry_time
  disabled     = false
}

# The deprecated mTLS endpoint, only if explicitly asked for.
#
# Legitimate reason to enable: you already depend on it and are migrating off.
# Not a legitimate reason: it appeared first in the provider docs.
resource "temporalcloud_metrics_endpoint" "legacy_promql" {
  count = var.enable_deprecated_promql_endpoint ? 1 : 0

  accepted_client_ca = var.promql_accepted_client_ca

  lifecycle {
    precondition {
      condition     = var.promql_accepted_client_ca != null
      error_message = "enable_deprecated_promql_endpoint requires promql_accepted_client_ca (base64-encoded PEM)."
    }
  }
}
