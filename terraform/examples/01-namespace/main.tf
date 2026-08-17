# A single Namespace with search attributes.
#
#   export TEMPORAL_CLOUD_API_KEY=<key>
#   terraform init && terraform plan

terraform {
  required_version = ">= 1.5"
  required_providers {
    temporalcloud = {
      source  = "temporalio/temporalcloud"
      version = "~> 1.7"
    }
  }
}

# Reads TEMPORAL_CLOUD_API_KEY from the environment. Never hardcode api_key here
# — it would be committed, and it is an account-level credential.
provider "temporalcloud" {}

module "orders" {
  source = "../../modules/namespace"

  name           = "orders-prod"
  regions        = ["aws-us-east-1"]
  retention_days = 30

  # Search attributes the duration SLO monitor in monitor/ relies on. Adding
  # them up front costs nothing; adding them during an incident is not possible.
  search_attributes = {
    OrderPriority = "Keyword"
    CustomerTier  = "Keyword"
    SubmittedAt   = "Datetime"
  }
}

output "endpoints" {
  value = module.orders.endpoints
}
