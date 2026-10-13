terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Deliberately no backend block. Pick one before anyone else applies this —
  # an S3 backend with encryption and a lock table is the usual choice. Local
  # state is acceptable for a single engineer's throwaway environment and for
  # nothing else.
  #
  # What lands in state matters less here than in terraform/ (SECURITY.md P4):
  # the RDS master password is managed by RDS in Secrets Manager and the
  # Temporal application password is written by scripts/deploy.sh, so neither
  # is ever in state. State still holds endpoints and ARNs — treat it as
  # internal, not secret.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = merge(var.tags, {
      "temporal-selfhosted/environment" = var.name
      "managed-by"                      = "terraform"
    })
  }
}
