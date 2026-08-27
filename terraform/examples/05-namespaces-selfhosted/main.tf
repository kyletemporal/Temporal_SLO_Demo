# Namespaces on demand against the SELF-HOSTED demo cluster.
#
# Run it:
#   cd demo && make up            # cluster on localhost:7233
#   cd ../terraform/examples/05-namespaces-selfhosted
#   terraform init
#   terraform plan                # reads live state; safe, changes nothing
#   terraform apply
#
# Adding a namespace is one block in `namespaces` below. That is the whole
# interface — no new directory, no new state, no copied module block.
#
# The fleet is defined as a variable DEFAULT rather than in a .tfvars file so
# this example is runnable straight from a clone. For real use, move it to
# terraform.tfvars (which .gitignore excludes, because tfvars is where secrets
# end up) or pass -var-file explicitly.

terraform {
  required_version = ">= 1.5"
}

module "namespaces" {
  source = "../../modules/namespace-selfhosted"

  address           = var.address
  default_retention = "168h"
  namespaces        = var.namespaces
}

variable "address" {
  description = "Temporal Frontend gRPC address."
  type        = string
  default     = "localhost:7233"
}

variable "namespaces" {
  type = map(object({
    retention         = optional(string)
    description       = optional(string, "")
    owner_email       = optional(string, "")
    allow_destroy     = optional(bool, false)
    data              = optional(map(string), {})
    search_attributes = optional(map(string), {})
  }))

  default = {
    # A team namespace with a long retention, because payments disputes arrive
    # weeks late and 72h of history cannot answer them.
    payments = {
      retention   = "720h"
      description = "Payments team — workflows handling charges and refunds"
      owner_email = "payments@example.com"
      data        = { team = "payments", tier = "1" }
      search_attributes = {
        OrderPriority = "Keyword"
        CustomerTier  = "Keyword"
      }
    }

    # Inherits default_retention (168h). Most namespaces should look like this.
    orders = {
      description = "Orders team"
      data        = { team = "orders", tier = "2" }
    }

    # Scratch space. The ONLY namespace with allow_destroy, and the only one
    # `terraform destroy` will actually remove — see the module README for why
    # that is the default.
    sandbox = {
      retention     = "24h"
      description   = "Scratch namespace — safe to delete"
      allow_destroy = true
      data          = { team = "platform", tier = "3" }
    }
  }
}

output "namespaces" {
  value = module.namespaces.namespaces
}

# Feed straight into the Prometheus rules and monitor config instead of
# maintaining the list in three places.
output "prometheus_namespace_regex" {
  value = module.namespaces.prometheus_namespace_regex
}
