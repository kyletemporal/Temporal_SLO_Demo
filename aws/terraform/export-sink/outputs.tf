output "bucket_name" {
  value = aws_s3_bucket.history.id
}

output "bucket_arn" {
  value = aws_s3_bucket.history.arn
}

output "role_arn" {
  description = "Feed this to `temporal cloud namespace export s3 validate` before trusting the sink."
  value       = aws_iam_role.export.arn
}

output "validate_command" {
  description = "Run this to confirm Temporal can actually assume the role and write. A sink that provisions is not a sink that works."
  value = join(" ", [
    "temporal cloud namespace export s3 validate",
    "--namespace ${var.namespace_id}",
    "--sink-name ${var.sink_name}",
    "--role-arn ${aws_iam_role.export.arn}",
    "--bucket-name ${aws_s3_bucket.history.id}",
    "--region ${var.region}",
  ])
}
