# PATTERN: rotate an API key with no downtime.
#
# THE PROBLEM: API keys must expire — the provider requires expiry_time and there
# is no "never". Replacing a key in place means the old one dies the moment
# Terraform applies, and every Worker still holding it fails to connect. Rotation
# done this way is an outage you scheduled yourself.
#
# THE SHAPE: two keys live at once. Deploy the new one, let Workers pick it up,
# then retire the old one in a SECOND apply. Overlap is what makes it safe.
#
#   1. active_key_slot = "a"        both keys exist, Workers use A
#   2. active_key_slot = "b"        both still exist, Workers move to B
#   3. retire_inactive_key = true   A is destroyed
#
# Three applies, each individually reversible. The alternative — one apply that
# swaps the key — has a window where a Worker restart cannot authenticate.

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

resource "temporalcloud_service_account" "workload" {
  name        = "orders-workload"
  description = "Managed by Terraform."

  namespace_accesses = [{
    namespace_id = var.namespace_id
    permission   = "Write"
  }]
}

resource "temporalcloud_apikey" "slot_a" {
  # count, not conditional attributes: retiring a key must DESTROY it, not
  # disable it. A disabled key is still a credential sitting in state.
  count = (var.retire_inactive_key && var.active_key_slot == "b") ? 0 : 1

  display_name = "orders-workload-a"
  description  = "Rotation slot A. Managed by Terraform."
  owner_type   = "service-account"
  owner_id     = temporalcloud_service_account.workload.id
  expiry_time  = var.slot_a_expiry
  disabled     = false
}

resource "temporalcloud_apikey" "slot_b" {
  count = (var.retire_inactive_key && var.active_key_slot == "a") ? 0 : 1

  display_name = "orders-workload-b"
  description  = "Rotation slot B. Managed by Terraform."
  owner_type   = "service-account"
  owner_id     = temporalcloud_service_account.workload.id
  expiry_time  = var.slot_b_expiry
  disabled     = false
}

# What Workers should be using right now. Wire this into your secret manager;
# the slot variable is the only thing that changes during a rotation.
output "active_api_key" {
  description = "The key Workers should hold. Feed to your secret manager."
  value = var.active_key_slot == "a" ? (
    length(temporalcloud_apikey.slot_a) > 0 ? temporalcloud_apikey.slot_a[0].token : null
    ) : (
    length(temporalcloud_apikey.slot_b) > 0 ? temporalcloud_apikey.slot_b[0].token : null
  )
  sensitive = true
}

output "rotation_state" {
  value = {
    active_slot   = var.active_key_slot
    slot_a_exists = length(temporalcloud_apikey.slot_a) > 0
    slot_b_exists = length(temporalcloud_apikey.slot_b) > 0
  }
}
