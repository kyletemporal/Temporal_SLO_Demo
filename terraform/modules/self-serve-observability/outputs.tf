output "folder_uid" {
  description = "Grafana folder UID for this team."
  value       = grafana_folder.team.uid
}

output "folder_url" {
  description = "Direct link to hand the team on day one."
  value       = grafana_folder.team.url
}

output "contact_point_name" {
  description = <<-EOT
    Contact point name.

    Exported because the PLATFORM team's notification policy may want to
    reference it. This module never creates a notification policy: that resource
    manages the entire tree and overwrites it, so a per-team copy would make each
    team's apply erase every other team's routing. Rules here route directly via
    notification_settings instead.
  EOT
  value       = grafana_contact_point.team.name
}

output "alert_rule_group" {
  description = "Name of the alert rule group provisioned for this team."
  value       = grafana_rule_group.team.name
}
