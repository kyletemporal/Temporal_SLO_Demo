environment = "dev"
service     = "orders"
regions     = ["aws-us-east-1"]

search_attributes = {
  OrderPriority = "Keyword"
}

tags = {
  cost_center = "engineering"
  owner       = "orders-team"
}
