# Onboard a team: Namespace, workload identity, credential, human access.

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

module "payments_team" {
  source = "../../modules/team-onboarding"

  team_name      = "payments"
  regions        = ["aws-us-east-1"]
  retention_days = 30

  # No 'never'. Rotation you cannot forget.
  apikey_expiry_time = "2027-01-01T00:00:00Z"

  team_members = {
    "lead@example.com" = { account_access = "Developer", namespace_permission = "Admin" }
    "dev@example.com"  = { account_access = "Developer", namespace_permission = "Write" }
    "sre@example.com"  = { account_access = "Read", namespace_permission = "Read" }
  }

  search_attributes = {
    PaymentMethod = "Keyword"
  }
}

# Read once, put in a secret manager, do not echo in CI:
#   terraform output -raw workload_api_key
output "workload_api_key" {
  value     = module.payments_team.workload_api_key
  sensitive = true
}
