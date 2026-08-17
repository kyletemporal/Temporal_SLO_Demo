# A Temporal Cloud Namespace with the settings that are hard to change later
# made explicit, and the ones that cost money made deliberate.
#
# Verified against provider schema v1.7.0, not against the docs — the published
# examples still show v0.0.6 and omit most of what this resource now supports.

terraform {
  required_version = ">= 1.5"
  required_providers {
    temporalcloud = {
      source  = "temporalio/temporalcloud"
      version = "~> 1.7"
    }
  }
}

resource "temporalcloud_namespace" "this" {
  name = var.name

  # A Namespace CANNOT change regions after creation. Getting this wrong means
  # creating a new Namespace and migrating every Workflow to it.
  regions = var.regions

  # Retention drives both cost and how far back you can investigate. It is also
  # the ceiling on any Visibility-based SLO: you cannot compute a 28-day
  # compliance window from 14 days of history.
  retention_days = var.retention_days

  # Exactly one auth method. API keys are simpler to rotate and are what the
  # OpenMetrics endpoint and most new integrations expect; mTLS is required if
  # you must pin client certificates.
  api_key_auth       = var.api_key_auth
  accepted_client_ca = var.accepted_client_ca

  # ATTRIBUTES, not blocks. In provider v1.7.0 certificate_filters and
  # codec_server are typed attributes (a list and a single nested object), so
  # the `dynamic "..." { }` block syntax the docs show for older versions fails
  # with "Blocks of type ... are not expected here". Caught by terraform
  # validate, which is why the examples run it.
  certificate_filters = var.certificate_filters
  codec_server        = var.codec_server

  timeouts {
    # Namespace creation is genuinely slow — the docs' own example shows 2m17s.
    # The default timeout is frequently too short for multi-region.
    create = var.create_timeout
    delete = var.delete_timeout
  }

  lifecycle {
    # Deleting a Namespace destroys every Workflow history in it. This is not
    # recoverable and it is far too easy to trigger with a rename: `name` forces
    # replacement, so a one-character typo in a variable would otherwise queue a
    # destroy/create of production.
    #
    # To intentionally delete, remove the guard in a separate, reviewed commit.
    prevent_destroy = true
  }
}

# Search attributes are how anything finds Workflows later — including the
# Visibility-based duration SLOs in monitor/. Adding them after the fact is
# cheap; discovering you need them mid-incident is not.
resource "temporalcloud_namespace_search_attribute" "this" {
  for_each = var.search_attributes

  namespace_id = temporalcloud_namespace.this.id
  name         = each.key
  type         = each.value
}
