# Onboard an application team onto the platform in one apply.
#
# This is the routine Temporal's own automation guidance describes: a new team
# needs a Namespace, an identity to connect with, a credential, and scoped
# permissions — and doing those by hand is where drift and over-permissioning
# come from.
#
# WHAT THIS DELIBERATELY DOES NOT DO: grant namespace access from the Namespace
# resource. Temporal's model attaches access to the IDENTITY (user or service
# account), not to the Namespace. There is no way to express it the other way
# round, and expecting to is a common first-time mistake.

terraform {
  required_version = ">= 1.5"
  required_providers {
    temporalcloud = {
      source  = "temporalio/temporalcloud"
      version = "~> 1.7"
    }
  }
}

module "namespace" {
  source = "../namespace"

  name              = var.team_name
  regions           = var.regions
  retention_days    = var.retention_days
  api_key_auth      = true
  search_attributes = var.search_attributes
  create_timeout    = var.create_timeout
}

# The workload identity. Least privilege by construction: no account_access is
# set, so this service account has NO account-level role and can only reach the
# namespaces granted below.
#
# Setting account_access here would be the mistake — an account role of `admin`
# or even `developer` grants far more than one team's workload needs, and
# account-level roles cannot be scoped to a namespace afterwards.
resource "temporalcloud_service_account" "workload" {
  name        = "${var.team_name}-workload"
  description = "Worker identity for the ${var.team_name} team. Managed by Terraform."

  namespace_accesses = [{
    namespace_id = module.namespace.id
    permission   = "Write"
  }]
}

# The credential.
#
# THE TOKEN LANDS IN TERRAFORM STATE. That is unavoidable — Terraform must know
# the value it created — and it is the single most important operational fact
# about this module. Treat state as a secret store: remote backend, encryption
# at rest, restricted access. If you cannot do that, create API keys with tcld
# instead and reference them, rather than managing them here.
resource "temporalcloud_apikey" "workload" {
  display_name = "${var.team_name}-workload"
  description  = "Managed by Terraform. Rotate by changing expiry_time."
  owner_type   = "service-account"
  owner_id     = temporalcloud_service_account.workload.id
  expiry_time  = var.apikey_expiry_time
  disabled     = false
}

# Human access, kept separate from the workload identity on purpose: people and
# workloads have different lifecycles, different blast radii, and different
# rotation stories. A shared credential for both is how a departed employee's
# access ends up embedded in a running Worker.
resource "temporalcloud_user" "team" {
  for_each = var.team_members

  email          = each.key
  account_access = each.value.account_access

  namespace_accesses = [{
    namespace_id = module.namespace.id
    permission   = each.value.namespace_permission
  }]

  lifecycle {
    # A user must be managed in EXACTLY ONE place. Terraform overwrites the full
    # permission set on every apply, so the same user declared in two modules
    # will have their access silently stomped by whichever applies last.
    precondition {
      condition     = each.value.account_access != "Owner"
      error_message = "Terraform cannot create or modify the Account Owner role. Import it if you need it in state, but manage it outside Terraform."
    }
  }
}
