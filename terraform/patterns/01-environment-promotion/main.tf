# PATTERN: one module, many environments, no copy-paste.
#
# THE PROBLEM: dev, staging and prod need the same Namespace shape with
# different sizes, regions and retention. Copying a directory per environment
# means a fix applied to prod that never reaches staging, and a staging config
# that quietly drifts into something prod is not.
#
# THE SHAPE: identical configuration, per-environment tfvars, separate state.
#
#   terraform workspace new prod
#   terraform apply -var-file=envs/prod.tfvars
#
# WORKSPACES OR DIRECTORIES? Workspaces share one backend key prefix and one set
# of credentials, which is convenient and is also the argument against them for
# production: an operator with staging access can `workspace select prod`. If
# your environments have different blast radii — and prod does — use separate
# backends and separate credentials, and keep this file as the shared module.

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

locals {
  # Environment is part of the name, not just a tag. A Namespace called
  # "orders" that you cannot tell apart from another "orders" in a different
  # account is how the wrong one gets terminated at 2am.
  namespace_name = "${var.service}-${var.environment}"

  # Retention is the ceiling on any Visibility-based SLO window, so it is
  # derived from the environment rather than set by hand — nobody remembers that
  # a 28-day compliance window needs 28+ days of history until it silently
  # cannot be computed.
  retention_days = var.environment == "prod" ? 30 : 7
}

module "namespace" {
  source = "../../modules/namespace"

  name           = local.namespace_name
  regions        = var.regions
  retention_days = local.retention_days
  api_key_auth   = true

  search_attributes = var.search_attributes
}

# Tags are how you attribute cost and find things later. Setting them from
# variables rather than literals is what stops prod being tagged as staging by
# a copy-paste.
resource "temporalcloud_namespace_tags" "this" {
  namespace_id = module.namespace.id

  tags = merge(var.tags, {
    environment = var.environment
    service     = var.service
    managed_by  = "terraform"
  })
}
