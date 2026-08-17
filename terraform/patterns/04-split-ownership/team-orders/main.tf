# The orders team's configuration. Applied with the ORDERS team's credentials,
# which cannot touch the platform state file.
#
# What this owns: identities and access scoped to its own Namespace.
# What it does NOT own: the Namespace itself, account-level roles, other teams.

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

# Consume the platform team's outputs rather than re-declaring the Namespace.
#
# Declaring it again in this state would give Terraform two owners for one
# resource, and the two would fight on every apply — each seeing the other's
# changes as drift to be corrected.
data "terraform_remote_state" "platform" {
  backend = "local"

  config = {
    path = "../platform/terraform.tfstate"
  }

  # In practice this is the shared remote backend, READ-ONLY for this team:
  # backend = "s3"
  # config = { bucket = "acme-tfstate", key = "temporal/platform.tfstate", region = "us-east-1" }
}

locals {
  namespace_id = data.terraform_remote_state.platform.outputs.orders_namespace_id
}

resource "temporalcloud_service_account" "workload" {
  name        = "orders-workload"
  description = "Orders team Worker identity. Managed by the orders team."

  # No account_access: a Worker needs its Namespace and nothing else. This is
  # also the boundary working as intended — this configuration's credentials
  # could not grant an account role even if someone asked it to.
  namespace_accesses = [{
    namespace_id = local.namespace_id
    permission   = "Write"
  }]
}

resource "temporalcloud_apikey" "workload" {
  display_name = "orders-workload"
  owner_type   = "service-account"
  owner_id     = temporalcloud_service_account.workload.id
  expiry_time  = var.apikey_expiry_time
  disabled     = false
}

output "workload_api_key" {
  value     = temporalcloud_apikey.workload.token
  sensitive = true
}
