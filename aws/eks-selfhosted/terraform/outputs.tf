# Everything scripts/deploy.sh needs, and nothing secret. The script reads these
# with `terraform output -json`.

output "region" {
  value = var.region
}

output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "kubeconfig_command" {
  description = "Run this to point kubectl at the cluster."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "db_host" {
  value = aws_db_instance.temporal.address
}

output "db_port" {
  value = aws_db_instance.temporal.port
}

output "db_master_username" {
  value = aws_db_instance.temporal.username
}

output "db_master_secret_arn" {
  description = "RDS-managed master credential. Used once, by the bootstrap Job."
  value       = aws_db_instance.temporal.master_user_secret[0].secret_arn
}

output "db_app_secret_arn" {
  description = "Secret container for the `temporal` role's password. Populated by scripts/deploy.sh."
  value       = aws_secretsmanager_secret.temporal_db.arn
}

output "db_instance_identifier" {
  value = aws_db_instance.temporal.identifier
}
