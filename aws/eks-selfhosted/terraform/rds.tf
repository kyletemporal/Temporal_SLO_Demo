# RDS PostgreSQL for BOTH Temporal stores.
#
# One instance, two databases: `temporal` (persistence) and
# `temporal_visibility` (SQL advanced visibility, PostgreSQL 12+ on server
# 1.20+). scripts/deploy.sh creates the databases and a least-privilege
# `temporal` role; the Helm chart's schema jobs then create the tables.
#
# Postgres for visibility is a deliberate choice for this environment, and it
# has a known limit: Temporal recommends Elasticsearch/OpenSearch for
# visibility "for any setup that spawns more than a few Workflow Executions".
# On SQL visibility, heavy List/Count traffic competes with history writes for
# the same instance. If a reproduction hinges on visibility load, that sharing
# is part of what is being reproduced — or a confound to remove by moving
# visibility to its own instance.

resource "aws_security_group" "rds" {
  name_prefix = "${var.name}-rds-"
  description = "PostgreSQL from EKS nodes only"
  vpc_id      = module.vpc.vpc_id

  lifecycle {
    create_before_destroy = true
  }
}

# With the VPC CNI's default mode, pods use their node's ENI and therefore the
# node security group. Both the module's node SG and the cluster primary SG are
# allowed so that this keeps working if a node group is later switched to the
# primary SG.
resource "aws_vpc_security_group_ingress_rule" "rds_from_nodes" {
  for_each = {
    node    = module.eks.node_security_group_id
    primary = module.eks.cluster_primary_security_group_id
  }

  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "PostgreSQL from EKS (${each.key} SG)"
}

resource "aws_db_parameter_group" "temporal" {
  name_prefix = "${var.name}-pg${var.postgres_engine_version}-"
  family      = "postgres${var.postgres_engine_version}"
  description = "Temporal on PostgreSQL ${var.postgres_engine_version}"

  # Reject any non-TLS connection. Temporal is configured for verified TLS
  # against the RDS CA (helm/values-temporal.yaml), so this costs nothing and
  # turns a silent plaintext fallback into a loud failure.
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  # Statements Temporal issues are short. Anything over 5s is a symptom worth
  # seeing in the RDS log rather than a query worth waiting on.
  parameter {
    name  = "log_min_duration_statement"
    value = "5000"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Created by Terraform BEFORE the instance, so Terraform owns it. Left to RDS,
# `enabled_cloudwatch_logs_exports` creates this group itself, outside
# Terraform, with no expiry — and it survived teardown, billing forever.
resource "aws_cloudwatch_log_group" "rds" {
  name              = "/aws/rds/instance/${var.name}/postgresql"
  retention_in_days = 7
}

resource "aws_db_instance" "temporal" {
  # A FIXED identifier, not identifier_prefix. If an apply is killed mid-create
  # (an idle CloudShell session does exactly that), the half-created instance
  # is not in state. With a prefix the re-run creates a SECOND instance and
  # orphans the first — billable, deletion-protected, invisible to destroy.
  # With a fixed name the re-run fails loudly on the name clash instead, and
  # `terraform import aws_db_instance.temporal <name>` recovers it.
  identifier = var.name

  engine         = "postgres"
  engine_version = var.postgres_engine_version
  instance_class = var.db_instance_class

  # Master user is for bootstrap only (creating the `temporal` role and
  # databases). RDS generates the password, stores it in Secrets Manager and
  # rotates it; it is never in Terraform state or tfvars.
  username                    = "temporal_admin"
  manage_master_user_password = true

  db_name = null # databases are created by scripts/deploy.sh with the right owner

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = module.vpc.database_subnet_group_name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  parameter_group_name = aws_db_parameter_group.temporal.name

  backup_retention_period  = var.db_backup_retention_days
  copy_tags_to_snapshot    = true
  delete_automated_backups = false

  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.name}-final-${formatdate("YYYYMMDDhhmm", plantimestamp())}"

  performance_insights_enabled          = true
  performance_insights_retention_period = 7
  enabled_cloudwatch_logs_exports       = ["postgresql"]

  depends_on = [aws_cloudwatch_log_group.rds]

  auto_minor_version_upgrade = true
  apply_immediately          = false

  lifecycle {
    # plantimestamp() changes every plan; the identifier only matters at
    # destroy time, so do not let it churn the resource.
    ignore_changes = [final_snapshot_identifier]
  }
}
