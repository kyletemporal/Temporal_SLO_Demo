# Workflow History Export to S3.
#
# WHY: Temporal Cloud retention caps at 90 days. Export writes CLOSED Workflow
# histories to your own bucket hourly, which is how you keep them for compliance,
# audit, or the ability to replay a Workflow in a debugger two years later.
#
# THREE THINGS THAT WILL BITE, ALL OF THEM BEFORE YOU GET TO THE INTERESTING PART
#
# 1. THE BUCKET MUST BE IN THE SAME REGION AS THE NAMESPACE. Not "should" —
#    Temporal's docs state it as a requirement. There is a precondition below
#    because getting it wrong produces a sink that provisions and never delivers.
#
# 2. THE IAM ROLE MUST EXIST BEFORE THE SINK. The Cloud UI offers a
#    CloudFormation flow that creates it for you, but Temporal's own docs say:
#    "Please pre-create the role if setting up Export via terraform/tcld." This
#    module creates it, in the right order, via depends_on.
#
# 3. THE TRUST PRINCIPAL IS AN INPUT, NOT A CONSTANT — and that is deliberate.
#    Temporal Cloud writes using MULTIPLE INTERMEDIARY IAM ROLES and picks among
#    them at random, for isolation and failover. That set is account- and
#    region-specific and Temporal can rotate it. Hardcoding a guess here would
#    produce a trust policy that works until it silently does not.
#
#    Get the real values from the CloudFormation template the Cloud UI generates
#    (Namespace → Export → Configure → Manual), and pass them in. See README.md.
#
# COST: each exported Workflow accrues one Action, billed per namespace. This is
# not free, and it scales with your Workflow volume rather than your data volume.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    temporalcloud = {
      source  = "temporalio/temporalcloud"
      version = "~> 1.7"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  bucket_name = var.bucket_name != null ? var.bucket_name : "temporal-history-${var.namespace_name}-${data.aws_caller_identity.current.account_id}"
  role_name   = "temporal-export-${var.namespace_name}"
}

resource "aws_s3_bucket" "history" {
  bucket = local.bucket_name

  # Exported history is an audit record. Deleting the bucket destroys it, and
  # the whole point of exporting was that Temporal's own retention expires.
  lifecycle {
    prevent_destroy = true
  }

  tags = merge(var.tags, {
    Purpose   = "temporal-workflow-history-export"
    Namespace = var.namespace_name
    ManagedBy = "terraform"
  })
}

# Exported history contains Workflow inputs and results — i.e. whatever your
# business data is. Public access here would be a data breach, not a
# misconfiguration.
resource "aws_s3_bucket_public_access_block" "history" {
  bucket                  = aws_s3_bucket.history.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "history" {
  bucket = aws_s3_bucket.history.id

  rule {
    apply_server_side_encryption_by_default {
      # KMS when a key is supplied, otherwise SSE-S3. If you pass a KMS key the
      # IAM role below is granted kms:GenerateDataKey — without that the export
      # fails at write time rather than at configuration time.
      sse_algorithm     = var.kms_key_arn != null ? "aws:kms" : "AES256"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = var.kms_key_arn != null
  }
}

resource "aws_s3_bucket_versioning" "history" {
  bucket = aws_s3_bucket.history.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Transition to cheaper storage rather than deleting. Export exists because you
# needed the data longer than 90 days; a lifecycle rule that expires it in 90
# days would quietly undo that.
resource "aws_s3_bucket_lifecycle_configuration" "history" {
  bucket     = aws_s3_bucket.history.id
  depends_on = [aws_s3_bucket_versioning.history]

  rule {
    id     = "archive-old-history"
    status = "Enabled"

    filter {}

    transition {
      days          = var.transition_to_ia_days
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = var.transition_to_glacier_days
      storage_class = "GLACIER"
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# The role Temporal Cloud assumes. Trust principals are inputs — see the header.
data "aws_iam_policy_document" "trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = var.temporal_cloud_principal_arns
    }

    # Confused-deputy protection. Without an ExternalId, anyone who learns your
    # role ARN and can get Temporal to assume roles could target your bucket.
    dynamic "condition" {
      for_each = var.external_id == null ? [] : [1]
      content {
        test     = "StringEquals"
        variable = "sts:ExternalId"
        values   = [var.external_id]
      }
    }
  }
}

data "aws_iam_policy_document" "write" {
  statement {
    sid    = "WriteHistory"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.history.arn}/*"]
  }

  statement {
    sid       = "ListBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.history.arn]
  }

  # Only when a KMS key is in play. Temporal's docs are explicit that adding a
  # KMS ARN later means updating this role too — a step that is easy to miss and
  # surfaces as export failures rather than a permissions error you would look for.
  dynamic "statement" {
    for_each = var.kms_key_arn == null ? [] : [1]
    content {
      sid       = "UseKmsKey"
      effect    = "Allow"
      actions   = ["kms:GenerateDataKey", "kms:Encrypt", "kms:DescribeKey"]
      resources = [var.kms_key_arn]
    }
  }
}

resource "aws_iam_role" "export" {
  name               = local.role_name
  description        = "Assumed by Temporal Cloud to write Workflow history to S3. Managed by Terraform."
  assume_role_policy = data.aws_iam_policy_document.trust.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "export" {
  name   = "temporal-export-write"
  role   = aws_iam_role.export.id
  policy = data.aws_iam_policy_document.write.json
}

# The sink itself. depends_on is load-bearing: Temporal validates the role at
# creation time, so a sink created before the role and policy exist fails.
resource "temporalcloud_namespace_export_sink" "history" {
  namespace = var.namespace_id
  sink_name = var.sink_name
  enabled   = var.enabled

  s3 = {
    aws_account_id = data.aws_caller_identity.current.account_id
    bucket_name    = aws_s3_bucket.history.id
    region         = var.region
    role_name      = aws_iam_role.export.name
    kms_arn        = var.kms_key_arn
  }

  depends_on = [
    aws_iam_role_policy.export,
    aws_s3_bucket_public_access_block.history,
  ]

  lifecycle {
    precondition {
      condition     = var.region == var.namespace_region
      error_message = "The S3 bucket region (${var.region}) must match the Namespace region (${var.namespace_region}). Temporal requires this, and a mismatch produces a sink that provisions successfully and never delivers data."
    }
  }
}
