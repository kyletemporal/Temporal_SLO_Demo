# PATTERN: split ownership — platform team owns the account, product teams own
# their namespaces. TWO STATE FILES, not one.
#
# THE PROBLEM: a single state file means every team that can change their own
# Namespace can also change account-level roles, delete another team's
# Namespace, and read every API key token in state. Terraform state is not
# access-controlled per resource — whoever can apply can read all of it.
#
# THE SHAPE: this configuration owns account-level resources and publishes
# Namespace IDs as outputs. Team configurations consume them via a remote state
# data source and own only what is inside their own Namespace.
#
# The boundary is real: a team's credentials cannot modify what is in this state
# file, because they never have them.

terraform {
  required_version = ">= 1.5"
  required_providers {
    temporalcloud = {
      source  = "temporalio/temporalcloud"
      version = "~> 1.7"
    }
  }

  # A remote backend is the point of this pattern, not an optional extra: the
  # boundary exists because the two states have different credentials.
  #
  # backend "s3" {
  #   bucket = "acme-tfstate"
  #   key    = "temporal/platform.tfstate"
  #   region = "us-east-1"
  #   encrypt = true          # state holds API key tokens in plaintext
  # }
}

provider "temporalcloud" {}

# Namespaces are created by the platform team, because creating one is an
# account-level act with cost and governance consequences.
module "orders" {
  source = "../../../modules/namespace"

  name           = "orders-prod"
  regions        = ["aws-us-east-1"]
  retention_days = 30
}

# Account-level human access lives here and only here. A user managed in two
# state files has their permissions overwritten by whichever applies last, with
# no error — which is why "one identity, one place" is a rule rather than advice.
resource "temporalcloud_user" "platform_admin" {
  email          = var.platform_admin_email
  account_access = "Admin"
}

# Published for team configurations to consume. Outputs are the API of this
# state file: teams get an ID to attach to, not the ability to change the
# Namespace it names.
output "orders_namespace_id" {
  value = module.orders.id
}

output "orders_namespace_endpoints" {
  value = module.orders.endpoints
}
