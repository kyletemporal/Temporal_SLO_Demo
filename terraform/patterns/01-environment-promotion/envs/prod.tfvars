environment = "prod"
service     = "orders"

# Prod is multi-region; dev is not. This is the difference that matters and it
# lives in data, not in a forked copy of the configuration.
regions = ["aws-us-east-1", "aws-us-west-2"]

search_attributes = {
  OrderPriority = "Keyword"
  CustomerTier  = "Keyword"
}

tags = {
  cost_center = "engineering"
  owner       = "orders-team"
  compliance  = "pci"
}
