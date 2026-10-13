# Plan-only checks that run with no AWS account: `terraform test`.
#
# The aws provider is mocked, so this proves the configuration plans and that
# the guard rails hold — not that AWS would accept it. `terraform apply` against
# a real account is the only test of that.

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["us-east-1a", "us-east-1b", "us-east-1c"]
    }
  }
  # IAM policy documents are rendered to JSON and parsed by the modules.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012", arn = "arn:aws:iam::123456789012:user/test" }
  }
  mock_data "aws_region" {
    defaults = { name = "us-east-1", region = "us-east-1" }
  }
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::123456789012:user/test" }
  }
}

mock_provider "tls" {}
mock_provider "time" {}
mock_provider "cloudinit" {}
mock_provider "null" {}

variables {
  admin_cidrs = ["203.0.113.10/32"]
}

run "plans_with_defaults" {
  command = plan

  assert {
    condition     = aws_db_instance.temporal.engine_version == "16"
    error_message = "PostgreSQL major must default to 16, the newest Temporal tests against."
  }
  assert {
    condition     = aws_db_instance.temporal.manage_master_user_password == true
    error_message = "The RDS master password must be RDS-managed, never in state."
  }
  assert {
    condition     = aws_db_instance.temporal.publicly_accessible == false && aws_db_instance.temporal.storage_encrypted == true
    error_message = "RDS must be private and encrypted."
  }
  assert {
    condition     = aws_db_instance.temporal.deletion_protection == true
    error_message = "Deletion protection must default on."
  }
  assert {
    condition     = one([for p in aws_db_parameter_group.temporal.parameter : p.value if p.name == "rds.force_ssl"]) == "1"
    error_message = "rds.force_ssl must be 1."
  }
}

run "rejects_open_api_endpoint" {
  command = plan
  variables {
    admin_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.admin_cidrs]
}

run "rejects_empty_admin_cidrs" {
  command = plan
  variables {
    admin_cidrs = []
  }
  expect_failures = [var.admin_cidrs]
}
