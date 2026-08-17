# PATTERN: a service boundary between teams, expressed as infrastructure.
#
# THE PROBLEM: two teams need to call each other's Workflows. The tempting
# answer — share a Namespace, or hand out cross-namespace credentials — couples
# their failure domains and their access control permanently.
#
# THE SHAPE: a Nexus Endpoint. The provider Namespace exposes an endpoint; caller
# Namespaces are named explicitly. Teams keep separate Namespaces, separate
# retention, separate blast radius, and the contract between them is a reviewable
# Terraform resource rather than a shared secret.
#
# WHY THIS BELONGS IN TERRAFORM: allowed_caller_namespaces IS the access control
# list. In a PR it is a diff someone approves. Done by hand in the UI it is an
# access grant with no record of who asked or why.

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

# The provider: owns the Workflows behind the endpoint.
module "payments" {
  source = "../../modules/namespace"

  name           = "payments-prod"
  regions        = ["aws-us-east-1"]
  retention_days = 30
}

# Callers: separate teams, separate Namespaces, possibly separate regions.
module "orders" {
  source = "../../modules/namespace"

  name           = "orders-prod"
  regions        = ["aws-us-east-1"]
  retention_days = 30
}

module "fulfilment" {
  source = "../../modules/namespace"

  name           = "fulfilment-prod"
  regions        = ["aws-us-west-2"]
  retention_days = 30
}

resource "temporalcloud_nexus_endpoint" "payments" {
  name = "payments-api"

  description = <<-EOT
    Payment operations exposed to other teams.

    Operations: charge, refund, void

    Treat this description as the contract. It is the first thing a caller team
    reads, and unlike a wiki page it is versioned with the grant itself.
  EOT

  # Where requests are routed. One worker_target only — a Nexus Endpoint fans in,
  # not out.
  worker_target = {
    namespace_id = module.payments.id
    task_queue   = "payments-nexus"
  }

  # THE ACCESS CONTROL LIST. Adding a team here is a reviewable diff.
  #
  # Note what is NOT possible: a caller cannot grant itself access from its own
  # configuration. The provider Namespace's config is the only place this can
  # change, which puts the decision with the team that owns the risk.
  allowed_caller_namespaces = [
    module.orders.id,
    module.fulfilment.id,
  ]
}

output "nexus_endpoint_id" {
  value = temporalcloud_nexus_endpoint.payments.id
}
